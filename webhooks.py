"""Webhook intake: verify first, then deduplicate transactionally.

Nothing is written to the inbox before the signature verifies. A duplicate
delivery applies once; the same event id with *different* content is quarantined.
Notifications never regress a state we already confirmed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from domain import canonical_json

# A verified event may not move a resource backwards out of these states.
TERMINAL_RESOURCE_STATES = ("COMPLETED", "SUCCEEDED", "REFUNDED")


@dataclass
class Verification:
    ok: bool
    reason: str


class MockVerifier:
    """Deterministic HMAC verifier used by the offline tests and mock mode."""

    def __init__(self, secret: Optional[str] = None):
        self.secret = (secret or os.getenv("PAYPILOT_WEBHOOK_SECRET", "mock-secret")).encode()

    def signature(self, body: bytes) -> str:
        return hmac.new(self.secret, body, hashlib.sha256).hexdigest()

    def verify(self, headers: Dict[str, str], body: bytes) -> Verification:
        supplied = (headers.get("paypal-transmission-sig") or headers.get("X-Signature") or "")
        if not supplied:
            return Verification(False, "MISSING_SIGNATURE")
        if not hmac.compare_digest(supplied, self.signature(body)):
            return Verification(False, "SIGNATURE_MISMATCH")
        return Verification(True, "VERIFIED")


class PayPalVerifier:
    """Real verifier: POST /v1/notifications/verify-webhook-signature.

    A verifier outage rejects the delivery; it never authorizes anything.
    """

    def __init__(self, client, webhook_id: Optional[str] = None):
        self.client = client
        self.webhook_id = webhook_id or os.getenv("PAYPAL_WEBHOOK_ID", "")

    def verify(self, headers: Dict[str, str], body: bytes) -> Verification:
        if not self.webhook_id:
            return Verification(False, "NO_WEBHOOK_ID_CONFIGURED")
        try:
            payload = {
                "auth_algo": headers.get("paypal-auth-algo", ""),
                "cert_url": headers.get("paypal-cert-url", ""),
                "transmission_id": headers.get("paypal-transmission-id", ""),
                "transmission_sig": headers.get("paypal-transmission-sig", ""),
                "transmission_time": headers.get("paypal-transmission-time", ""),
                "webhook_id": self.webhook_id,
                "webhook_event": json.loads(body.decode("utf-8")),
            }
            data = self.client.verify_webhook_signature(payload)
            status = (data or {}).get("verification_status")
            return Verification(status == "SUCCESS", str(status or "NO_STATUS"))
        except Exception as exc:                      # outage / network
            return Verification(False, "VERIFIER_UNAVAILABLE:" + type(exc).__name__)


def ingest(storage, verifier, headers: Dict[str, str], body: bytes, mode: str,
           client=None, now: Optional[datetime] = None) -> Dict[str, Any]:
    ts = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(
        microsecond=0).isoformat()
    verification = verifier.verify(headers, body)
    if not verification.ok:
        return {"status": "rejected", "reason": verification.reason}     # nothing stored

    try:
        event = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {"status": "rejected", "reason": "MALFORMED_BODY"}

    event_id = str(event.get("id") or event.get("event_id") or "")
    event_type = str(event.get("event_type") or "")
    if not event_id:
        return {"status": "rejected", "reason": "MISSING_EVENT_ID"}
    content_hash = hashlib.sha256(canonical_json(event).encode("utf-8")).hexdigest()

    with storage.tx():
        row = storage.conn.execute(
            "SELECT content_hash, applied_at FROM webhook_inbox WHERE mode=? AND event_id=?",
            (mode, event_id)).fetchone()
        if row:
            if row["content_hash"] == content_hash:
                return {"status": "duplicate", "event_id": event_id,
                        "applied_at": row["applied_at"]}
            return {"status": "quarantined", "event_id": event_id,
                    "reason": "SAME_EVENT_ID_DIFFERENT_CONTENT"}
        storage.conn.execute(
            "INSERT INTO webhook_inbox (mode, event_id, content_hash, payload, received_at)"
            " VALUES (?,?,?,?,?)", (mode, event_id, content_hash, canonical_json(event), ts))

    applied = apply_event(storage, client, event, event_type, ts, mode)
    with storage.tx():
        storage.conn.execute(
            "UPDATE webhook_inbox SET applied_at=? WHERE mode=? AND event_id=?",
            (ts, mode, event_id))
    return {"status": "applied", "event_id": event_id, "event_type": event_type,
            "applied": applied}


def apply_event(storage, client, event: Dict[str, Any], event_type: str, ts: str,
                mode: str) -> Dict[str, Any]:
    """Query the resource the event refers to; never regress a confirmed state."""
    resource = (event.get("resource") or {})
    resource_id = resource.get("id") or resource.get("capture_id") or resource.get("batch_id")
    if not resource_id:
        return {"note": "no resource id in event"}
    existing = storage.get_resource(resource_id)
    if existing and existing["status"] in TERMINAL_RESOURCE_STATES and mode == "mock":
        return {"note": "ignored: state already terminal", "status": existing["status"]}

    status = resource.get("status") or "UNKNOWN"
    kind = event_type.split(".")[0] if event_type else "unknown"
    with storage.tx():
        storage.upsert_resource(resource_id, kind=kind, status=status,
                                parent_id=resource.get("order_id")
                                or resource.get("capture_id"),
                                snapshot={"event_type": event_type, "status": status})
    return {"resource_id": resource_id, "status": status}
