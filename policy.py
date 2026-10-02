"""The policy engine: a pure function that decides whether a financial write may
proceed. It performs no I/O, holds no state, and can be exhaustively tested with
a fake clock.

The model is never consulted here and cannot reach this code path with a value
that widens its own authority: limits, windows, currencies and recipients all
come from `config` and from administrator-controlled records.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

import config
from domain import (
    Action,
    Approval,
    Decision,
    LimitCheck,
    Operation,
    PolicyResult,
    PolicySnapshot,
    canonical_json,
)

# Reason codes (stable strings — the audit record is a public interface).
UNKNOWN_ACTION = "UNKNOWN_ACTION"
UNEXPECTED_FIELDS = "UNEXPECTED_FIELDS"
UNAUTHORIZED_ACTOR = "UNAUTHORIZED_ACTOR"
UNSUPPORTED_MODE = "UNSUPPORTED_MODE"
MISSING_REFERENCE = "MISSING_REFERENCE"
MISSING_REASON = "MISSING_REASON"
REASON_TOO_LONG = "REASON_TOO_LONG"
INVALID_AMOUNT = "INVALID_AMOUNT"
NONFINITE_AMOUNT = "NONFINITE_AMOUNT"
ZERO_AMOUNT = "ZERO_AMOUNT"
NEGATIVE_AMOUNT = "NEGATIVE_AMOUNT"
EXCESSIVE_PRECISION = "EXCESSIVE_PRECISION"
AMOUNT_OVERFLOW = "AMOUNT_OVERFLOW"
UNSUPPORTED_CURRENCY = "UNSUPPORTED_CURRENCY"
SUPPLIED_RECIPIENT = "SUPPLIED_RECIPIENT"
UNLISTED_RECIPIENT = "UNLISTED_RECIPIENT"
DUPLICATE_SUPPRESSED = "DUPLICATE_SUPPRESSED"
DUPLICATE_CONFLICT = "DUPLICATE_CONFLICT"
TRANSACTION_LIMIT = "TRANSACTION_LIMIT"
WINDOW_24H_LIMIT = "WINDOW_24H_LIMIT"
VELOCITY_LIMIT = "VELOCITY_LIMIT"
APPROVAL_MISSING = "APPROVAL_MISSING"
APPROVAL_EXPIRED = "APPROVAL_EXPIRED"
APPROVAL_ARGUMENT_MISMATCH = "APPROVAL_ARGUMENT_MISMATCH"
APPROVAL_ACTOR_MISMATCH = "APPROVAL_ACTOR_MISMATCH"
ALL_CHECKS_PASSED = "ALL_CHECKS_PASSED"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _parse_iso(value: str) -> datetime:
    text = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _parse_amount(amount_raw: Optional[str]) -> Tuple[Optional[int], Optional[str]]:
    """Returns (minor_units, error_code). The only place money is parsed."""
    if amount_raw is None:
        return None, INVALID_AMOUNT
    try:
        value = Decimal(str(amount_raw).strip())
    except (InvalidOperation, ValueError):
        return None, INVALID_AMOUNT
    if not value.is_finite():
        return None, NONFINITE_AMOUNT
    if value == 0:
        return None, ZERO_AMOUNT
    if value < 0:
        return None, NEGATIVE_AMOUNT
    exponent = value.as_tuple().exponent
    if isinstance(exponent, int) and exponent < -config.MAX_AMOUNT_PRECISION:
        return None, EXCESSIVE_PRECISION
    minor = int((value * 100).to_integral_value())
    if minor > config.MAX_AMOUNT_MINOR:
        return None, AMOUNT_OVERFLOW
    return minor, None


def _record(operation: Operation, snapshot: PolicySnapshot, amount_minor: Optional[int],
            approval: Optional[Approval], decision: str, reason_codes: List[str],
            limit_checks: List[LimitCheck], reservation_delta: int,
            duplicate_of: Optional[str]) -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "decision_id": "dec_" + uuid.uuid4().hex[:16],
        "run_id": operation.run_id,
        "operation_id": operation.operation_id,
        "attempt": 1,
        "at": snapshot.now_iso,
        "actor_id": operation.actor_id,
        "mode": operation.mode,
        "action": operation.action,
        "amount_minor": amount_minor,
        "currency": operation.currency,
        "recipient_hash": operation.recipient_hash,
        "resource_id": operation.resource_id,
        "reason": operation.reason,
        "arguments_hash": operation.arguments_hash,
        "policy_version": config.POLICY_VERSION,
        "snapshot_version": snapshot.snapshot_version,
        "approval_id": approval.approval_id if approval else None,
        "duplicate_of": duplicate_of,
        "limit_checks": [c.to_dict() for c in limit_checks],
        "reservation_delta_minor": reservation_delta,
        "decision": decision,
        "reason_codes": reason_codes,
    }


def _deny(operation: Operation, snapshot: PolicySnapshot, approval: Optional[Approval],
          code: str, amount_minor: Optional[int] = None,
          limit_checks: Optional[List[LimitCheck]] = None) -> PolicyResult:
    checks = limit_checks or []
    return PolicyResult(
        decision=Decision.DENY,
        reason_codes=[code],
        amount_minor=amount_minor,
        reservation_delta_minor=0,
        limit_checks=checks,
        record=_record(operation, snapshot, amount_minor, approval, Decision.DENY,
                       [code], checks, 0, None),
    )


def evaluate_policy(operation: Operation,
                    snapshot: PolicySnapshot,
                    approval: Optional[Approval],
                    now: datetime) -> PolicyResult:
    """Evaluate in the fixed order documented in docs/v2/ARCHITECTURE.md §2."""

    # 1. Unknown actions, fields, modes, actors.
    if operation.action not in config.ALLOWED_ACTIONS:
        return _deny(operation, snapshot, approval, UNKNOWN_ACTION)
    if operation.extra_fields:
        return _deny(operation, snapshot, approval, UNEXPECTED_FIELDS)
    if operation.actor_id != config.LOCAL_ACTOR_ID:
        return _deny(operation, snapshot, approval, UNAUTHORIZED_ACTOR)
    if operation.mode not in ("mock", "sandbox"):
        return _deny(operation, snapshot, approval, UNSUPPORTED_MODE)
    if operation.action in (Action.CAPTURE, Action.REFUND) and not operation.resource_id:
        return _deny(operation, snapshot, approval, MISSING_REFERENCE)

    # 2. Reason: required, bounded, treated as data.
    reason = (operation.reason or "").strip()
    if not reason:
        return _deny(operation, snapshot, approval, MISSING_REASON)
    if len(reason) > config.MAX_REASON_CHARS:
        return _deny(operation, snapshot, approval, REASON_TOO_LONG)

    # 3. Amount.
    needs_amount = operation.action in (Action.CREATE_ORDER, Action.REFUND, Action.PAYOUT)
    amount_minor: Optional[int] = None
    if needs_amount:
        amount_minor, err = _parse_amount(operation.amount_raw)
        if err:
            return _deny(operation, snapshot, approval, err)
    elif operation.amount_raw not in (None, "", "0", "0.00"):
        # capture must not carry a caller-supplied amount: it is derived from the
        # verified order.
        return _deny(operation, snapshot, approval, UNEXPECTED_FIELDS)

    # 3b. Currency must be one an administrator enabled; the model cannot add one.
    if operation.currency not in config.SUPPORTED_CURRENCIES:
        return _deny(operation, snapshot, approval, UNSUPPORTED_CURRENCY, amount_minor)
    # 4. Recipient identity is resolved from the allowlist, never accepted.
    if operation.supplied_recipient:
        return _deny(operation, snapshot, approval, SUPPLIED_RECIPIENT, amount_minor)
    if operation.action == Action.PAYOUT:
        if not operation.recipient_alias:
            return _deny(operation, snapshot, approval, UNLISTED_RECIPIENT, amount_minor)
        listed = snapshot.allowlist.get(operation.recipient_alias)
        if not listed or (operation.recipient_hash and listed != operation.recipient_hash):
            return _deny(operation, snapshot, approval, UNLISTED_RECIPIENT, amount_minor)

    # 5. Duplicate business events.
    if snapshot.duplicate_conflict:
        return _deny(operation, snapshot, approval, DUPLICATE_CONFLICT, amount_minor)
    if snapshot.duplicate_operation_id:
        # Identical business event already recorded: return its stored status
        # instead of moving money twice.
        return PolicyResult(
            decision=Decision.ALLOW,
            reason_codes=[DUPLICATE_SUPPRESSED],
            amount_minor=amount_minor,
            reservation_delta_minor=0,
            limit_checks=[],
            record=_record(operation, snapshot, amount_minor, approval, Decision.ALLOW,
                           [DUPLICATE_SUPPRESSED], [], 0, snapshot.duplicate_operation_id),
        )

    # Reservation: money leaving the account is reserved; an incoming capture and
    # an order creation are not (order creation never double-counts exposure).
    reservation_delta = amount_minor if operation.action in (Action.REFUND, Action.PAYOUT) else 0

    window_start = _iso(now - timedelta(seconds=config.WINDOW_24H_SECONDS))
    window_end = _iso(now)
    velocity_start = _iso(now - timedelta(seconds=config.VELOCITY_WINDOW_SECONDS))
    checks: List[LimitCheck] = []

    # 6. Per-transaction ceiling.
    limit = config.PER_TRANSACTION_LIMITS_MINOR.get(operation.action)
    if limit is not None:
        observed = amount_minor or 0
        check = LimitCheck("PER_TRANSACTION", None, None, observed, observed, limit,
                           observed <= limit)
        checks.append(check)
        if not check.passed:
            return _deny(operation, snapshot, approval, TRANSACTION_LIMIT, amount_minor, checks)

    # 7. Rolling 24h ceiling, counting completed + reserved + unknown exposure.
    limit_24h = config.WINDOW_24H_LIMITS_MINOR.get(operation.action)
    if limit_24h is not None:
        observed = (snapshot.completed_24h_minor.get(operation.action, 0)
                    + snapshot.reserved_24h_minor.get(operation.action, 0)
                    + snapshot.unknown_24h_minor.get(operation.action, 0))
        check = LimitCheck("WINDOW_24H", window_start, window_end, observed,
                           reservation_delta, limit_24h,
                           observed + reservation_delta <= limit_24h)
        checks.append(check)
        if not check.passed:
            return _deny(operation, snapshot, approval, WINDOW_24H_LIMIT, amount_minor, checks)

    # 8. Velocity: logical operations, not attempts.
    velocity = LimitCheck("VELOCITY_60S", velocity_start, window_end,
                          snapshot.recent_operation_count, 1,
                          config.VELOCITY_LIMIT_COUNT,
                          snapshot.recent_operation_count + 1 <= config.VELOCITY_LIMIT_COUNT)
    checks.append(velocity)
    if not velocity.passed:
        return _deny(operation, snapshot, approval, VELOCITY_LIMIT, amount_minor, checks)

    # 9. Approval binds the immutable arguments of money-out operations.
    if operation.action in config.APPROVAL_REQUIRED_ACTIONS:
        if approval is None:
            record = _record(operation, snapshot, amount_minor, approval,
                             Decision.REQUIRE_APPROVAL, [APPROVAL_MISSING], checks,
                             reservation_delta, None)
            return PolicyResult(Decision.REQUIRE_APPROVAL, [APPROVAL_MISSING], amount_minor,
                                reservation_delta, checks, record)
        if approval.arguments_hash != operation.arguments_hash:
            return _deny(operation, snapshot, approval, APPROVAL_ARGUMENT_MISMATCH,
                         amount_minor, checks)
        if approval.actor_id != operation.actor_id:
            return _deny(operation, snapshot, approval, APPROVAL_ACTOR_MISMATCH,
                         amount_minor, checks)
        if _parse_iso(approval.expires_at) <= now:
            return _deny(operation, snapshot, approval, APPROVAL_EXPIRED, amount_minor, checks)

    # 10. Every applicable check passed.
    return PolicyResult(
        decision=Decision.ALLOW,
        reason_codes=[ALL_CHECKS_PASSED],
        amount_minor=amount_minor,
        reservation_delta_minor=reservation_delta,
        limit_checks=checks,
        record=_record(operation, snapshot, amount_minor, approval, Decision.ALLOW,
                       [ALL_CHECKS_PASSED], checks, reservation_delta, None),
    )


def policy_snapshot_document(snapshot: PolicySnapshot) -> str:
    """Canonical rendering of a snapshot — used for evidence and hashing."""
    return canonical_json({
        "policy_version": snapshot.policy_version,
        "mode": snapshot.mode,
        "snapshot_version": snapshot.snapshot_version,
        "now": snapshot.now_iso,
        "window_24h_start": snapshot.window_24h_start_iso,
        "velocity_window_start": snapshot.velocity_window_start_iso,
        "completed_24h_minor": snapshot.completed_24h_minor,
        "reserved_24h_minor": snapshot.reserved_24h_minor,
        "unknown_24h_minor": snapshot.unknown_24h_minor,
        "recent_operation_count": snapshot.recent_operation_count,
        "duplicate_operation_id": snapshot.duplicate_operation_id,
        "duplicate_conflict": snapshot.duplicate_conflict,
        "allowlist_aliases": sorted(snapshot.allowlist.keys()),
    })
