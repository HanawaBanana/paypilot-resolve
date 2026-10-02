"""PayPilot Resolve — the local operator console.

Everything the UI shows is read back from the database: the plan, the policy
decision records, the approvals, the audit timeline and the platform ids. The UI
cannot execute anything itself; it asks the orchestrator to advance a run.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from flask import Flask, jsonify, request, send_from_directory

import agent
import config
import executor
import orchestrator
import storage as storage_module
import webhooks

app = Flask(__name__, static_folder="static", static_url_path="")


def _mode() -> str:
    return os.getenv("PAYPILOT_MODE", config.MODE)


def build(mode=None):
    mode = mode or _mode()
    store = storage_module.Storage(path="paypilot-%s.sqlite3" % mode)
    store.init_schema()
    if mode == "sandbox":
        from paypal_client import PayPalClient

        client = PayPalClient(mode="sandbox")
    else:
        from mock_paypal import MockPayPal

        client = MockPayPal()
    ex = executor.Executor(store, client)
    planner = agent.Planner()
    orch = orchestrator.Orchestrator(store, ex, planner, client)
    return store, client, ex, orch


STORE, CLIENT, EXECUTOR, ORCH = build()


def _now():
    return datetime.now(timezone.utc)


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/return")
def buyer_return():
    """Where PayPal sends the buyer back after approving in the sandbox."""
    order_id = request.args.get("token") or request.args.get("order_id") or ""
    if not order_id:
        return "PayPal came back without an order id.", 400
    if _mode() == "sandbox" and hasattr(CLIENT, "get_order"):
        try:
            order = CLIENT.get_order(order_id)
        except Exception as exc:
            return "Order %s could not be verified: %s" % (order_id, exc), 502
        return ("<h2>Buyer approval received</h2><p>Order <code>%s</code> is now "
                "<b>%s</b>.</p><p>Return to the PayPilot Resolve console and press "
                "<b>Advance</b>: the capture is bound to this verified order, and the "
                "refund and payout follow it.</p>" % (order_id, order.get("status")))
    return "<h2>Approval received (mock)</h2>"


@app.route("/cancel")
def buyer_cancel():
    return "<h2>Checkout cancelled</h2><p>Nothing was captured.</p>"


@app.route("/api/health")
def health():
    return jsonify({
        "ok": True,
        "mode": _mode(),
        "planner": "llm" if agent.Planner().client.configured else "rules",
        "policy_version": config.POLICY_VERSION,
        "limits": {
            "per_transaction_minor": config.PER_TRANSACTION_LIMITS_MINOR,
            "window_24h_minor": config.WINDOW_24H_LIMITS_MINOR,
            "velocity_per_60s": config.VELOCITY_LIMIT_COUNT,
        },
    })


@app.route("/api/runs", methods=["POST"])
def create_run():
    body = request.get_json(silent=True) or {}
    goal = (body.get("goal") or "").strip()
    if not goal:
        return jsonify({"ok": False, "error": "empty goal"}), 400
    run_id = ORCH.start(goal, _mode(), _now())
    first = ORCH.advance(run_id, _now())
    second = ORCH.advance(run_id, _now())
    return jsonify({"ok": True, "run_id": run_id, "transitions": [first, second],
                    "view": ORCH.view(run_id)})


@app.route("/api/runs/<run_id>/advance", methods=["POST"])
def advance(run_id):
    body = request.get_json(silent=True) or {}
    result = ORCH.advance(run_id, _now(), stop_after=body.get("stop_after"))
    return jsonify({"ok": True, "transition": result, "view": ORCH.view(run_id)})


@app.route("/api/runs/<run_id>/approve", methods=["POST"])
def approve(run_id):
    body = request.get_json(silent=True) or {}
    result = ORCH.approve(run_id, body.get("session_id") or "local-session", _now())
    return jsonify({"ok": bool(result.get("ok")), "approval": result,
                    "view": ORCH.view(run_id)})


@app.route("/api/runs/<run_id>/buyer-approve", methods=["POST"])
def buyer_approve(run_id):
    """Mock mode only: stands in for the buyer approving in PayPal's UI."""
    if _mode() != "mock":
        return jsonify({"ok": False, "error": "only available in mock mode",
                        "hint": "in sandbox mode approve the order at the PayPal checkout"}), 400
    operation = STORE.get_operation_by_step(run_id, "s1")
    if not operation or not operation["platform_id"]:
        return jsonify({"ok": False, "error": "no order for this run yet"}), 400
    order = CLIENT.buyer_approve(operation["platform_id"])
    return jsonify({"ok": True, "order": order, "view": ORCH.view(run_id)})


@app.route("/api/runs/<run_id>/view")
def view(run_id):
    return jsonify(ORCH.view(run_id))


@app.route("/api/audit/verify")
def audit_verify():
    from audit import verify_chain

    intact, broken = verify_chain(STORE.conn)
    return jsonify({"intact": intact, "first_broken_seq": broken,
                    "note": "hash-chained: detects edits, not administrator-proof"})


@app.route("/api/webhooks", methods=["POST"])
def receive_webhook():
    headers = {k.lower(): v for k, v in request.headers.items()}
    verifier = (webhooks.MockVerifier() if _mode() == "mock"
                else webhooks.PayPalVerifier(CLIENT))
    result = webhooks.ingest(STORE, verifier, headers, request.get_data(),
                             _mode(), CLIENT, _now())
    status = 200 if result["status"] in ("applied", "duplicate") else 202
    if result["status"] == "rejected":
        status = 400
    return jsonify(result), status


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8100"))
    print("PayPilot Resolve on http://localhost:%d  (mode=%s, planner=%s)"
          % (port, _mode(), "llm" if agent.Planner().client.configured else "rules"))
    # single-threaded: one SQLite connection, one operator, deterministic evidence
    app.run(host="127.0.0.1", port=port, debug=False, threaded=False)
