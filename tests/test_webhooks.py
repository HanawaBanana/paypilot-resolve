"""Webhook intake tests — architecture test plan items 26-29."""

import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import webhooks  # noqa: E402
from mock_paypal import MockPayPal  # noqa: E402
from storage import Storage  # noqa: E402

NOW = datetime(2026, 10, 2, 22, 0, tzinfo=timezone.utc)


def setup(tmp_path):
    storage = Storage(str(tmp_path / "w.sqlite3"))
    storage.init_schema()
    return storage, MockPayPal(), webhooks.MockVerifier("test-secret")


def event(event_id="WH-1", capture_id="CAP-1", status="COMPLETED", event_type="PAYMENT.CAPTURE.COMPLETED"):
    body = {"id": event_id, "event_type": event_type,
            "resource": {"id": capture_id, "status": status}}
    return json.dumps(body).encode()


def signed(verifier, body):
    return {"paypal-transmission-sig": verifier.signature(body)}


def test_26_signature_failure_rejects_the_webhook(tmp_path):
    storage, client, verifier = setup(tmp_path)
    body = event()
    result = webhooks.ingest(storage, verifier, {"paypal-transmission-sig": "deadbeef"},
                             body, "mock", client, NOW)
    assert result["status"] == "rejected"
    assert result["reason"] == "SIGNATURE_MISMATCH"
    stored = storage.conn.execute("SELECT COUNT(*) AS c FROM webhook_inbox").fetchone()["c"]
    assert stored == 0                      # nothing is stored before verification


def test_missing_signature_is_rejected(tmp_path):
    storage, client, verifier = setup(tmp_path)
    assert webhooks.ingest(storage, verifier, {}, event(), "mock", client, NOW)["status"] == "rejected"


def test_27_verifier_outage_rejects_the_webhook(tmp_path):
    storage, client, _ = setup(tmp_path)

    class BrokenVerifier:
        def verify(self, headers, body):
            return webhooks.Verification(False, "VERIFIER_UNAVAILABLE:Timeout")

    result = webhooks.ingest(storage, BrokenVerifier(), {"paypal-transmission-sig": "x"},
                             event(), "mock", client, NOW)
    assert result["status"] == "rejected"
    assert "VERIFIER_UNAVAILABLE" in result["reason"]


def test_28_duplicate_delivery_applies_once(tmp_path):
    storage, client, verifier = setup(tmp_path)
    body = event()
    headers = signed(verifier, body)
    first = webhooks.ingest(storage, verifier, headers, body, "mock", client, NOW)
    second = webhooks.ingest(storage, verifier, headers, body, "mock", client, NOW)
    assert first["status"] == "applied"
    assert second["status"] == "duplicate"
    rows = storage.conn.execute("SELECT COUNT(*) AS c FROM webhook_inbox").fetchone()["c"]
    assert rows == 1


def test_same_event_id_with_different_content_is_quarantined(tmp_path):
    storage, client, verifier = setup(tmp_path)
    body = event()
    webhooks.ingest(storage, verifier, signed(verifier, body), body, "mock", client, NOW)
    conflicting = event(capture_id="CAP-OTHER")
    result = webhooks.ingest(storage, verifier, signed(verifier, conflicting), conflicting,
                             "mock", client, NOW)
    assert result["status"] == "quarantined"
    assert result["reason"] == "SAME_EVENT_ID_DIFFERENT_CONTENT"


def test_29_out_of_order_webhook_cannot_regress_state(tmp_path):
    storage, client, verifier = setup(tmp_path)
    done = event(event_id="WH-2", status="COMPLETED")
    webhooks.ingest(storage, verifier, signed(verifier, done), done, "mock", client, NOW)
    assert storage.get_resource("CAP-1")["status"] == "COMPLETED"

    stale = event(event_id="WH-3", status="PENDING")
    webhooks.ingest(storage, verifier, signed(verifier, stale), stale, "mock", client, NOW)
    # a late notification does not walk the resource backwards
    assert storage.get_resource("CAP-1")["status"] == "COMPLETED"
