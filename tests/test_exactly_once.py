"""Exactly-once / idempotency tests.

Covers items 20-25 of `docs/v2/ARCHITECTURE.md` §6. Everything runs against the
deterministic local platform: no network, no credentials, injected clock.
"""

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import policy  # noqa: E402
from domain import Action, Approval, Decision, Operation, OpState  # noqa: E402
from executor import Executor  # noqa: E402
from mock_paypal import MockPayPal  # noqa: E402
from storage import Storage  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
config.RECIPIENTS_PATH = os.path.join(REPO, "recipients.json")

NOW = datetime(2026, 10, 2, 22, 0, 0, tzinfo=timezone.utc)
ALIAS = "collaborator"


def setup(tmp_path):
    storage = Storage(str(tmp_path / "test.sqlite3"))
    storage.init_schema()
    client = MockPayPal()
    return storage, client, Executor(storage, client)


def operation(action=Action.CREATE_ORDER, amount="12.00", operation_id="op-1",
              reason="Collect for the approved review", resource=None, step="step-1"):
    alias = ALIAS if action == Action.PAYOUT else None
    return Operation(
        operation_id=operation_id,
        run_id="run-1",
        step_id=step,
        action=action,
        amount_raw=amount,
        currency="USD",
        reason=reason,
        mode="mock",
        actor_id=config.LOCAL_ACTOR_ID,
        recipient_alias=alias,
        recipient_hash=Storage.allowlist().get(ALIAS) if alias else None,
        resource_id=resource,
    )


def approval_for(op, expires_in=600):
    return Approval(approval_id="apr-1", run_id=op.run_id, plan_hash="plan-hash",
                    arguments_hash=op.arguments_hash, actor_id=op.actor_id,
                    approved_at=(NOW - timedelta(seconds=30)).isoformat(),
                    expires_at=(NOW + timedelta(seconds=expires_in)).isoformat(),
                    session_id="sess-1")


# --------------------------------------------------------------------- 20 / 21
def test_20_identical_business_event_returns_the_existing_operation(tmp_path):
    storage, client, executor = setup(tmp_path)
    first = executor.admit(operation(operation_id="op-a"), now=NOW)
    assert first["decision"] == Decision.ALLOW

    again = executor.admit(operation(operation_id="op-b"), now=NOW)
    assert again["reason_codes"] == [policy.DUPLICATE_SUPPRESSED]
    assert again["duplicate_of"] == "op-a"
    assert storage.get_operation("op-b") is None          # nothing new was persisted
    assert executor.dispatch("op-a", now=NOW)["ok"] is True
    # the replay still reports the original operation's status
    third = executor.admit(operation(operation_id="op-c"), now=NOW)
    assert third["existing_state"] == OpState.SUCCEEDED
    assert client.calls_for("create_order") == 1          # one order, not three


def test_21_conflicting_reuse_of_a_business_event_is_denied(tmp_path):
    storage, client, executor = setup(tmp_path)
    executor.admit(operation(operation_id="op-a", reason="Collect for the approved review"),
                   now=NOW)
    clash = executor.admit(
        operation(operation_id="op-b", reason="A different justification entirely"), now=NOW)
    assert clash["decision"] == Decision.DENY
    assert clash["reason_codes"] == [policy.DUPLICATE_CONFLICT]
    assert storage.get_operation("op-b") is None


# -------------------------------------------------------------------------- 22
def test_22_timeout_retry_preserves_the_key_and_the_payload(tmp_path):
    storage, client, executor = setup(tmp_path)
    client._failures.append("create_order:transport")
    executor.admit(operation(operation_id="op-a"), now=NOW)

    first = executor.dispatch("op-a", now=NOW)
    assert first["state"] == OpState.DISPATCHED           # ambiguous, reservation retained
    assert first["ok"] is False

    second = executor.dispatch("op-a", now=NOW)
    assert second["ok"] is True and second["state"] == OpState.SUCCEEDED

    calls = [c for c in client.calls if c["op"] == "create_order"]
    assert len(calls) == 2
    assert len({c["request_id"] for c in calls}) == 1      # identical key on every attempt
    assert len(client.orders) == 1                         # one resource, not two
    assert storage.get_operation("op-a")["attempts"] == 2


# -------------------------------------------------------------------------- 23
def test_23_crash_after_accept_reconciles_without_repaying(tmp_path):
    storage, client, executor = setup(tmp_path)
    executor.admit(operation(operation_id="op-order"), now=NOW)
    executor.dispatch("op-order", now=NOW)
    order_id = storage.get_operation("op-order")["platform_id"]
    client.buyer_approve(order_id)

    capture = operation(action=Action.CAPTURE, amount=None, operation_id="op-cap",
                        resource=order_id)
    admitted = executor.admit(capture, now=NOW)
    assert admitted["decision"] == Decision.ALLOW          # capture needs no approval

    client.fail_after_accept("capture")                    # platform accepted, reply lost
    ambiguous = executor.dispatch("op-cap", now=NOW)
    assert ambiguous["ok"] is False
    assert ambiguous["state"] == OpState.DISPATCHED
    assert "AMBIGUOUS_RESERVATION_RETAINED" in ambiguous["note"]
    assert client.calls_for("capture_order") == 1

    outcome = executor.reconcile_operation("op-cap", now=NOW)
    assert outcome["outcome"] == "MATCHED"
    assert storage.get_operation("op-cap")["state"] == OpState.SUCCEEDED
    assert storage.get_operation("op-cap")["platform_id"] == outcome["platform_id"]
    assert client.calls_for("capture_order") == 1          # reconciliation paid nothing

    # a later dispatch is a no-op, never a second capture
    repeat = executor.dispatch("op-cap", now=NOW)
    assert repeat["note"] == "ALREADY_SUCCEEDED_NO_REPAYMENT"
    assert client.calls_for("capture_order") == 1


# -------------------------------------------------------------------------- 24
def test_24_expired_retention_forbids_blind_replay(tmp_path):
    storage, client, executor = setup(tmp_path)
    long_ago = NOW - timedelta(seconds=config.REQUEST_ID_RETENTION_SECONDS + 120)
    client._failures = ["create_order:transport"] * config.MAX_DISPATCH_ATTEMPTS
    executor.admit(operation(operation_id="op-old"), now=long_ago)

    result = None
    for _ in range(config.MAX_DISPATCH_ATTEMPTS):
        result = executor.dispatch("op-old", now=long_ago)
    assert result["state"] == OpState.UNKNOWN

    outcome = executor.reconcile_operation("op-old", now=NOW)
    assert outcome["outcome"] == "NOT_FOUND"
    assert outcome["reason"] == "RETENTION_EXPIRED"
    assert outcome["replay_allowed"] is False

    refused = executor.dispatch("op-old", now=NOW)
    assert refused["ok"] is False
    assert refused["error"] == "ATTEMPT_LIMIT_REACHED"
    assert refused["note"] == "FAILED_CLOSED_NO_BLIND_REPLAY"
    assert client.calls_for("create_order") == config.MAX_DISPATCH_ATTEMPTS


def test_unknown_outcome_keeps_its_reservation(tmp_path):
    storage, client, executor = setup(tmp_path)
    client._failures = ["payout:transport"] * config.MAX_DISPATCH_ATTEMPTS
    payout = operation(action=Action.PAYOUT, amount="3.00", operation_id="op-pay",
                       reason="Collaborator share")
    admitted = executor.admit(payout, approval=approval_for(payout), now=NOW)
    assert admitted["decision"] == Decision.ALLOW
    for _ in range(config.MAX_DISPATCH_ATTEMPTS):
        result = executor.dispatch("op-pay", now=NOW)
    assert result["state"] == OpState.UNKNOWN

    exposure = storage.exposure(Action.PAYOUT, (NOW - timedelta(hours=1)).isoformat())
    assert exposure["unknown"] == 300                       # exposure is not silently released
    snap = storage.build_snapshot(payout, NOW)
    assert snap.unknown_24h_minor[Action.PAYOUT] == 300


# -------------------------------------------------------------------------- 25
def test_25_payout_sender_identifiers_stay_stable_across_retries(tmp_path):
    storage, client, executor = setup(tmp_path)
    payout = operation(action=Action.PAYOUT, amount="3.00", operation_id="op-pay",
                       reason="Collaborator share")
    executor.admit(payout, approval=approval_for(payout), now=NOW)
    client._failures.append("payout:transport")

    executor.dispatch("op-pay", now=NOW)
    executor.dispatch("op-pay", now=NOW)

    calls = [c for c in client.calls if c["op"] == "payout"]
    assert len(calls) == 2
    assert len({c["request_id"] for c in calls}) == 1
    assert len({c["sender_batch_id"] for c in calls}) == 1
    assert len({c["sender_item_id"] for c in calls}) == 1
    assert len(client.batches) == 1
    assert storage.get_operation("op-pay")["state"] == OpState.SUCCEEDED
    # the alias, not the address, is what the operation rows carry
    assert storage.get_operation("op-pay")["recipient_alias"] == ALIAS
    assert "example.com" not in (storage.get_operation("op-pay")["canonical_payload"] or "")


# ------------------------------------------------------------ shared invariants
def test_audit_chain_is_intact_after_the_whole_flow(tmp_path):
    storage, client, executor = setup(tmp_path)
    from audit import verify_chain

    executor.admit(operation(operation_id="op-order"), now=NOW)
    executor.dispatch("op-order", now=NOW)
    order_id = storage.get_operation("op-order")["platform_id"]
    client.buyer_approve(order_id)
    executor.admit(operation(action=Action.CAPTURE, amount=None, operation_id="op-cap",
                             resource=order_id), now=NOW)
    executor.dispatch("op-cap", now=NOW)

    intact, broken = verify_chain(storage.conn)
    assert intact is True and broken is None


def test_tampering_with_an_audit_row_is_detected(tmp_path):
    storage, client, executor = setup(tmp_path)
    from audit import verify_chain

    executor.admit(operation(operation_id="op-order"), now=NOW)
    executor.dispatch("op-order", now=NOW)
    assert verify_chain(storage.conn)[0] is True

    storage.conn.execute("UPDATE audit_events SET payload=? WHERE seq=1", ('{"edited":true}',))
    intact, broken = verify_chain(storage.conn)
    assert intact is False and broken == 1
