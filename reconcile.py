"""Reconciliation: decide what actually happened, from the platform's own records.

Reconciliation never moves money. It either proves a completed outcome (so the
executor can stop without repaying) or refuses to guess and raises a human-review
card. Reporting absence is never treated as proof of failure.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

import config
from domain import OpState

MATCHED = "MATCHED"
NOT_FOUND = "NOT_FOUND"
MISMATCH = "MISMATCH"
UNVERIFIABLE = "UNVERIFIABLE"


def _age_seconds(row: Any, now: datetime) -> float:
    created = datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00"))
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return (now - created).total_seconds()


def reconcile(storage, client, operation_id: str,
              now: Optional[datetime] = None) -> Dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    row = storage.get_operation(operation_id)
    if row is None:
        return {"outcome": UNVERIFIABLE, "reason": "UNKNOWN_OPERATION"}

    hit = client.find_by_request_id(row["request_id"])
    if hit.get("found"):
        record = hit.get("record") or {}
        amount = record.get("amount")
        expected = row["amount_raw"]
        if expected and amount and str(amount) != str(expected):
            return {"outcome": MISMATCH, "reason": "AMOUNT_MISMATCH",
                    "expected": expected, "found": amount,
                    "platform_id": record.get("id")}
        if record.get("currency") and record["currency"] != row["currency"]:
            return {"outcome": MISMATCH, "reason": "CURRENCY_MISMATCH",
                    "platform_id": record.get("id")}
        return {"outcome": MATCHED, "platform_id": record.get("id"),
                "status": record.get("status"), "record": record}

    age = _age_seconds(row, now)
    if age <= config.REQUEST_ID_RETENTION_SECONDS:
        return {"outcome": NOT_FOUND, "reason": "NO_PLATFORM_RECORD_WITHIN_RETENTION",
                "age_seconds": int(age), "replay_allowed": True}
    return {"outcome": NOT_FOUND, "reason": "RETENTION_EXPIRED", "age_seconds": int(age),
            "replay_allowed": False}
