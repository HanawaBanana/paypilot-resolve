"""Immutable domain contracts.

Money is carried as a *string* from the outside world and converted to integer
minor units only by the policy engine, so that every rounding/precision decision
happens in one auditable place.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# --------------------------------------------------------------------- actions
class Action:
    CREATE_ORDER = "create_order"
    CAPTURE = "capture"
    REFUND = "refund"
    PAYOUT = "payout"


class Decision:
    ALLOW = "ALLOW"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
    DENY = "DENY"


class OpState:
    PLANNED = "PLANNED"
    RESERVED = "RESERVED"
    DISPATCHED = "DISPATCHED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class RunState:
    PLANNING = "PLANNING"
    VALIDATING = "VALIDATING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    EXECUTING = "EXECUTING"
    WAITING_BUYER = "WAITING_BUYER"
    VERIFYING = "VERIFYING"
    RECONCILING = "RECONCILING"
    SUCCEEDED = "SUCCEEDED"
    DENIED = "DENIED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"
    MANUAL_REVIEW = "MANUAL_REVIEW"


TERMINAL_RUN_STATES = (
    RunState.SUCCEEDED,
    RunState.DENIED,
    RunState.CANCELLED,
    RunState.FAILED,
    RunState.MANUAL_REVIEW,
)


def canonical_json(obj: Any) -> str:
    """Deterministic JSON — the basis of every hash in this system."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def arguments_hash(action: str, amount_raw: Optional[str], currency: str,
                   recipient_alias: Optional[str], resource_id: Optional[str],
                   reason: str) -> str:
    """Hash of the immutable argument binding an approval is tied to."""
    return sha256_hex(canonical_json({
        "action": action,
        "amount": amount_raw,
        "currency": currency,
        "recipient_alias": recipient_alias,
        "resource_id": resource_id,
        "reason": reason,
    }))


def business_fingerprint(action: str, amount_raw: Optional[str], currency: str,
                         recipient_hash: Optional[str],
                         resource_id: Optional[str]) -> str:
    """Identifies the *business event* (not the attempt) for duplicate detection."""
    return sha256_hex(canonical_json({
        "action": action,
        "amount": amount_raw,
        "currency": currency,
        "recipient_hash": recipient_hash,
        "resource_id": resource_id,
    }))


# ------------------------------------------------------------------ contracts
@dataclass(frozen=True)
class Operation:
    """One financial write, before it is authorized."""

    operation_id: str
    run_id: str
    step_id: str
    action: str
    amount_raw: Optional[str]
    currency: str
    reason: str
    mode: str
    actor_id: str
    recipient_alias: Optional[str] = None
    recipient_hash: Optional[str] = None
    resource_id: Optional[str] = None
    # Raw recipient supplied by the model/user; any value here is a denial.
    supplied_recipient: Optional[str] = None
    # Fields the planner is not allowed to invent.
    extra_fields: Dict[str, Any] = field(default_factory=dict)

    @property
    def arguments_hash(self) -> str:
        return arguments_hash(self.action, self.amount_raw, self.currency,
                              self.recipient_alias, self.resource_id, self.reason)

    @property
    def fingerprint(self) -> str:
        return business_fingerprint(self.action, self.amount_raw, self.currency,
                                    self.recipient_hash, self.resource_id)


@dataclass(frozen=True)
class Approval:
    approval_id: str
    run_id: str
    plan_hash: str
    arguments_hash: str
    actor_id: str
    approved_at: str          # ISO-8601 UTC
    expires_at: str           # ISO-8601 UTC
    session_id: str


@dataclass(frozen=True)
class LimitCheck:
    rule: str
    window_start: Optional[str]
    window_end: Optional[str]
    observed_minor: int
    proposed_delta_minor: int
    limit_minor: int
    passed: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rule": self.rule,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "observed_minor": self.observed_minor,
            "proposed_delta_minor": self.proposed_delta_minor,
            "limit_minor": self.limit_minor,
            "passed": self.passed,
        }


@dataclass(frozen=True)
class PolicySnapshot:
    """Everything the pure evaluator is allowed to look at. No I/O happens there."""

    policy_version: str
    mode: str
    snapshot_version: int
    now_iso: str
    window_24h_start_iso: str
    velocity_window_start_iso: str
    completed_24h_minor: Dict[str, int] = field(default_factory=dict)
    reserved_24h_minor: Dict[str, int] = field(default_factory=dict)
    unknown_24h_minor: Dict[str, int] = field(default_factory=dict)
    recent_operation_count: int = 0
    duplicate_operation_id: Optional[str] = None
    duplicate_conflict: bool = False
    allowlist: Dict[str, str] = field(default_factory=dict)   # alias -> hash


@dataclass(frozen=True)
class PolicyResult:
    decision: str
    reason_codes: List[str]
    amount_minor: Optional[int]
    reservation_delta_minor: int
    limit_checks: List[LimitCheck]
    record: Dict[str, Any]

    @property
    def allowed(self) -> bool:
        return self.decision == Decision.ALLOW
