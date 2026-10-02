"""Headless walkthrough of the resolution flow.

    .venv/bin/python demo.py              # mock platform, deterministic
    PAYPILOT_MODE=sandbox .venv/bin/python demo.py   # real PayPal sandbox

It prints the same artefacts the UI shows: the proposed plan, the policy
decision record, the approval, the execution timeline, the deliberate
interruption and the resume that proves nothing is paid twice.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

import config


def build(mode: str):
    """Wire the app together for one mode. Keeps the demo honest about which
    platform it is talking to."""
    import agent
    import executor
    import orchestrator
    import storage

    store = storage.Storage(mode=mode) if False else storage.Storage(
        path=("paypilot-%s.sqlite3" % mode))
    store.init_schema()
    if mode == "mock":
        from mock_paypal import MockPayPal
        client = MockPayPal()
    else:
        from paypal_client import PayPalClient
        client = PayPalClient(mode="sandbox")
    ex = executor.Executor(store, client)
    planner = agent.Planner()
    return store, client, ex, orchestrator.Orchestrator(store, ex, planner, client)


def run(goal: str, mode: str = "mock", interrupt_after: str = "s3", buyer_auto: bool = True):
    store, client, ex, orch = build(mode)
    now = datetime.now(timezone.utc)
    run_id = orch.start(goal, mode, now)
    print("== run", run_id, "| mode", mode)

    print("-- advance (plan)"); print(json.dumps(orch.advance(run_id, now), indent=1)[:900])
    print("-- advance (validate)"); print(json.dumps(orch.advance(run_id, now), indent=1)[:900])
    approval = orch.approve(run_id, "demo-session", now)
    print("-- approve:", json.dumps(approval)[:400])

    # order -> buyer approves in PayPal -> capture -> refund, then interrupt
    for i in range(6):
        step = orch.advance(run_id, now, stop_after=interrupt_after)
        print("-- step", i, step.get("state"), json.dumps(step.get("executed", []))[:200])
        if step.get("interrupted"):
            print("!! deliberate interruption after", interrupt_after)
            break
        if step.get("state") == "WAITING_BUYER":
            if buyer_auto and mode == "mock":
                order_id = store.get_operation_by_step(run_id, "s1")["platform_id"]
                client.buyer_approve(order_id)
                print("-- buyer approved order", order_id)
            else:
                print("-- waiting for the human buyer at the PayPal checkout")
                break
        if step.get("state") in ("SUCCEEDED", "FAILED", "DENIED", "MANUAL_REVIEW"):
            break

    print("== resuming (this is the point of the design)")
    seen = None
    for i in range(8):
        step = orch.advance(run_id, now)
        print("-- resume", i, step.get("state"), json.dumps(step.get("executed", []))[:200])
        if step.get("state") in ("SUCCEEDED", "FAILED", "DENIED", "MANUAL_REVIEW"):
            break
        fingerprint = json.dumps(orch.view(run_id).get("operations"), sort_keys=True)
        if fingerprint == seen:
            print("!! no further progress", step.get("state"))
            break
        seen = fingerprint
    view = orch.view(run_id)
    print("== receipt")
    print(json.dumps({"state": view["run"]["state"],
                      "operations": [{k: o[k] for k in ("step_id", "action", "state",
                                                        "amount", "platform_id")}
                                     for o in view["operations"]]}, indent=1))
    return view


if __name__ == "__main__":
    goal = sys.argv[1] if len(sys.argv) > 1 else (
        "Collect $120 for the delivered UX audit and competitor scan. "
        "The client dropped the competitor scan, so refund $30 and pay $40 "
        "to the approved collaborator.")
    run(goal, mode=config.MODE)
