"""The executor: the only component allowed to move money.

It needs a database-backed admission (an allowed policy decision, a persisted
operation, a reservation and an audit record) before it will dispatch anything.
Request ids and payout batch ids are persisted at admission, so every retry —
including one that happens after a restart — carries the identical key.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import config
import policy
from audit import append_event
from domain import Action, Decision, Operation, OpState
from mock_paypal import PayPalPlatformError, PayPalTransportError
from reconcile import MATCHED, reconcile


def _now(now: Optional[datetime] = None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0)


class Executor:
    def __init__(self, storage, client):
        self.storage = storage
        self.client = client

    # ------------------------------------------------------------------ admit
    def admit(self, operation: Operation, approval=None,
              now: Optional[datetime] = None) -> Dict[str, Any]:
        """Policy + persistence in one write transaction. No network happens here."""
        now = _now(now)
        with self.storage.tx():
            snapshot = self.storage.build_snapshot(operation, now)
            result = policy.evaluate_policy(operation, snapshot, approval, now)
            append_event(self.storage.conn, operation.run_id, "policy_evaluated",
                         result.record, snapshot.now_iso, operation.operation_id)

            if result.decision != Decision.ALLOW:
                return {"decision": result.decision, "record": result.record,
                        "operation_id": operation.operation_id,
                        "reason_codes": result.reason_codes}

            if result.reason_codes == [policy.DUPLICATE_SUPPRESSED]:
                existing = self.storage.get_operation(snapshot.duplicate_operation_id)
                return {"decision": Decision.ALLOW, "duplicate_of": snapshot.duplicate_operation_id,
                        "record": result.record, "operation_id": operation.operation_id,
                        "existing_state": existing["state"] if existing else None,
                        "platform_id": existing["platform_id"] if existing else None,
                        "reason_codes": result.reason_codes}

            request_id = "req-" + uuid.uuid4().hex
            self.storage.create_operation(operation, request_id, OpState.RESERVED, now,
                                          amount_minor=result.amount_minor)
            if result.reservation_delta_minor:
                self.storage.reserve(operation.operation_id, operation.action,
                                     result.reservation_delta_minor, now)
            self.storage.enqueue(operation.operation_id, "execute",
                                 {"action": operation.action}, now)
            append_event(self.storage.conn, operation.run_id, "operation_admitted",
                         {"request_id": request_id,
                          "amount_minor": result.amount_minor,
                          "reservation_minor": result.reservation_delta_minor},
                         snapshot.now_iso, operation.operation_id)
            return {"decision": Decision.ALLOW, "record": result.record,
                    "operation_id": operation.operation_id, "request_id": request_id,
                    "reason_codes": result.reason_codes}

    # --------------------------------------------------------------- dispatch
    def dispatch(self, operation_id: str, now: Optional[datetime] = None) -> Dict[str, Any]:
        now = _now(now)
        row = self.storage.get_operation(operation_id)
        if row is None:
            return {"ok": False, "error": "UNKNOWN_OPERATION"}

        if row["state"] == OpState.SUCCEEDED:
            return {"ok": True, "state": OpState.SUCCEEDED, "platform_id": row["platform_id"],
                    "note": "ALREADY_SUCCEEDED_NO_REPAYMENT", "attempts": row["attempts"]}

        if row["attempts"] >= config.MAX_DISPATCH_ATTEMPTS:
            with self.storage.tx():
                self.storage.set_operation_state(operation_id, OpState.UNKNOWN, None, now)
                self.storage.settle_reservation(operation_id, OpState.UNKNOWN, now)
                append_event(self.storage.conn, row["run_id"], "dispatch_refused",
                             {"reason": "ATTEMPT_LIMIT_REACHED", "attempts": row["attempts"]},
                             now.isoformat(), operation_id)
            return {"ok": False, "state": OpState.UNKNOWN, "error": "ATTEMPT_LIMIT_REACHED",
                    "attempts": row["attempts"], "note": "FAILED_CLOSED_NO_BLIND_REPLAY"}

        with self.storage.tx():
            attempts = self.storage.mark_attempt(operation_id, now)
            self.storage.set_operation_state(operation_id, OpState.DISPATCHED, None, now)
            append_event(self.storage.conn, row["run_id"], "operation_dispatched",
                         {"attempt": attempts, "request_id": row["request_id"]}, now.isoformat(),
                         operation_id)

        try:
            result, platform_id = self._call(row, now)
        except PayPalPlatformError as exc:
            with self.storage.tx():
                self.storage.set_operation_state(operation_id, OpState.FAILED, None, now)
                self.storage.settle_reservation(operation_id, OpState.FAILED, now)
                append_event(self.storage.conn, row["run_id"], "operation_failed",
                             {"error": str(exc), "attempt": attempts}, now.isoformat(),
                             operation_id)
            return {"ok": False, "state": OpState.FAILED, "error": str(exc), "attempts": attempts}
        except PayPalTransportError as exc:
            exhausted = attempts >= config.MAX_DISPATCH_ATTEMPTS
            with self.storage.tx():
                self.storage.set_operation_state(
                    operation_id, OpState.UNKNOWN if exhausted else OpState.DISPATCHED, None, now)
                if exhausted:
                    self.storage.settle_reservation(operation_id, OpState.UNKNOWN, now)
                append_event(self.storage.conn, row["run_id"], "operation_ambiguous",
                             {"error": str(exc), "attempt": attempts,
                              "state": OpState.UNKNOWN if exhausted else OpState.DISPATCHED},
                             now.isoformat(), operation_id)
            return {"ok": False, "state": OpState.UNKNOWN if exhausted else OpState.DISPATCHED,
                    "error": str(exc), "attempts": attempts,
                    "note": "AMBIGUOUS_RESERVATION_RETAINED"}

        with self.storage.tx():
            self.storage.set_operation_state(operation_id, OpState.SUCCEEDED, platform_id, now)
            self.storage.settle_reservation(operation_id, OpState.SUCCEEDED, now)
            append_event(self.storage.conn, row["run_id"], "operation_succeeded",
                         {"platform_id": platform_id, "attempt": attempts,
                          "result": result}, now.isoformat(), operation_id)
        return {"ok": True, "state": OpState.SUCCEEDED, "platform_id": platform_id,
                "attempts": attempts, "result": result}

    def _call(self, row, now: datetime):
        action = row["action"]
        request_id = row["request_id"]
        if action == Action.CREATE_ORDER:
            data = self.client.create_order(row["amount_raw"], row["currency"],
                                            row["run_id"], request_id)
            return data, data["id"]
        if action == Action.CAPTURE:
            data = self.client.capture_order(row["resource_id"], request_id)
            return data, data["capture"]["id"]
        if action == Action.REFUND:
            data = self.client.refund(row["resource_id"], row["amount_raw"], row["currency"],
                                      request_id)
            return data, data["id"]
        if action == Action.PAYOUT:
            address = self.storage.resolve_recipient(row["recipient_alias"])
            if not address:
                raise PayPalPlatformError("RECIPIENT_NOT_RESOLVABLE")
            # deterministic, persisted identifiers: a retry reuses the same batch
            short = row["operation_id"].replace("op-", "")[:16]
            data = self.client.payout(address, row["amount_raw"], row["currency"],
                                      "BATCH-" + short, "ITEM-" + short, request_id)
            return data, data["item"]["id"]
        raise PayPalPlatformError("UNSUPPORTED_ACTION")

    # ------------------------------------------------------------ reconcile in
    def reconcile_operation(self, operation_id: str,
                            now: Optional[datetime] = None) -> Dict[str, Any]:
        now = _now(now)
        outcome = reconcile(self.storage, self.client, operation_id, now)
        row = self.storage.get_operation(operation_id)
        with self.storage.tx():
            if outcome["outcome"] == MATCHED:
                self.storage.set_operation_state(operation_id, OpState.SUCCEEDED,
                                                 outcome.get("platform_id"), now)
                self.storage.settle_reservation(operation_id, OpState.SUCCEEDED, now)
            elif not outcome.get("replay_allowed", False):
                self.storage.set_operation_state(operation_id, OpState.UNKNOWN, None, now)
            append_event(self.storage.conn, row["run_id"], "reconciled", outcome,
                         now.isoformat(), operation_id)
        return outcome
