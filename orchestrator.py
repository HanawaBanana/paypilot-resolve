"""The orchestrator: the state machine that ties a proposal to a verified outcome.

Contract: `advance(run_id)` performs at most one transition and is safe to call
again at any time (it is the resume path). Resource references inside a plan are
resolved **only** from verified local records — never from text the model wrote.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import config
from audit import append_event
from domain import (
    Action,
    Approval,
    Decision,
    Operation,
    OpState,
    RunState,
    TERMINAL_RUN_STATES,
    canonical_json,
    sha256_hex,
)
from executor import Executor


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def plan_hash(steps: List[Dict[str, Any]]) -> str:
    return sha256_hex(canonical_json(steps))


class Orchestrator:
    def __init__(self, storage, executor: Executor, planner, client):
        self.storage = storage
        self.executor = executor
        self.planner = planner
        self.client = client

    # ------------------------------------------------------------------- start
    def start(self, goal: str, mode: str = "mock",
              now: Optional[datetime] = None) -> str:
        now = now or datetime.now(timezone.utc)
        run_id = "run-" + uuid.uuid4().hex[:12]
        with self.storage.tx():
            self.storage.create_run(run_id, goal, mode, now, RunState.PLANNING)
            append_event(self.storage.conn, run_id, "run_started",
                         {"goal": goal, "mode": mode}, _iso(now))
        return run_id

    # --------------------------------------------------------------- plan step
    def _plan(self, run_id: str, now: datetime) -> Dict[str, Any]:
        run = self.storage.get_run(run_id)
        proposal = self.planner.plan(run["goal"], self._observations(run_id))
        # Defence in depth: the plan contract is enforced at this boundary too, so
        # a planner implementation that skips its own validation cannot smuggle a
        # raw address, an unknown tool or a dangling dependency into the plan.
        from agent import validate_steps
        validated, rejected = validate_steps(proposal.steps)
        if rejected:
            proposal.notes = list(proposal.notes) + ["orchestrator rejected: " + r for r in rejected]
        if [s["step_id"] for s in validated] != [s["step_id"] for s in proposal.steps]:
            proposal.notes = list(proposal.notes) + ["plan pruned to %d step(s)" % len(validated)]
        proposal.steps = validated
        digest = plan_hash(proposal.steps)
        with self.storage.tx():
            version = self.storage.save_plan(run_id, digest, proposal.steps, now)
            append_event(self.storage.conn, run_id, "plan_proposed",
                         {"source": proposal.source, "model_calls": proposal.model_calls,
                          "plan_hash": digest, "version": version,
                          "notes": proposal.notes,
                          "steps": [{"step_id": s["step_id"], "action": s["action"],
                                     "reason": s["reason"]} for s in proposal.steps]},
                         _iso(now))
            if not proposal.steps:
                self.storage.set_run_state(run_id, RunState.FAILED,
                                           "planner produced no usable steps", now)
            else:
                self.storage.set_run_state(run_id, RunState.VALIDATING, "", now)
        return {"state": RunState.VALIDATING if proposal.steps else RunState.FAILED,
                "plan_hash": digest, "source": proposal.source, "version": version,
                "notes": proposal.notes}

    def _observations(self, run_id: str) -> List[Dict[str, Any]]:
        out = []
        for op in self.storage.operations_for_run(run_id):
            out.append({"step_id": op["step_id"], "action": op["action"],
                        "state": op["state"], "platform_id": op["platform_id"]})
        return out

    # -------------------------------------------------------------- validation
    def _resolve_resource(self, run_id: str, ref: Optional[str]) -> Optional[str]:
        """`$s2.capture_id` -> the verified platform id recorded for step s2."""
        if not ref or not isinstance(ref, str):
            return None
        if not ref.startswith("$"):
            return None                      # a literal id from the model is never trusted
        step_id = ref[1:].split(".", 1)[0]
        row = self.storage.conn.execute(
            "SELECT platform_id, state FROM operations WHERE run_id=? AND step_id=?",
            (run_id, step_id)).fetchone()
        if not row or not row["platform_id"]:
            return None
        if row["state"] != OpState.SUCCEEDED:
            return None
        return row["platform_id"]

    def _dependencies_satisfied(self, run_id: str, step) -> bool:
        """Every declared dependency must be verified before this step may run."""
        dependency = step["depends_on"]
        if not dependency:
            return True
        row = self.storage.conn.execute(
            "SELECT state FROM steps WHERE run_id=? AND step_id=?",
            (run_id, dependency)).fetchone()
        return bool(row) and row["state"] == "SUCCEEDED"

    def _bindable(self, step) -> bool:
        return step["action"] in config.APPROVAL_REQUIRED_ACTIONS

    def _operation_for_step(self, run_id: str, step, now: datetime,
                            require_resource: bool = False) -> Optional[Operation]:
        args = {}
        try:
            import json
            args = json.loads(step["args_json"]) if step["args_json"] else {}
        except ValueError:
            args = {}
        ref = args.get("resource")
        resource = self._resolve_resource(run_id, ref)
        if require_resource and step["action"] in (Action.CAPTURE, Action.REFUND) and not resource:
            return None                      # dependency not verified yet -> not ready
        return Operation(
            operation_id="op-" + uuid.uuid4().hex[:12],
            run_id=run_id,
            step_id=step["step_id"],
            action=step["action"],
            amount_raw=args.get("amount"),
            currency=args.get("currency") or "USD",
            reason=step["reason"],
            mode=self.storage.get_run(run_id)["mode"],
            actor_id=config.LOCAL_ACTOR_ID,
            recipient_alias=args.get("recipient_alias"),
            recipient_hash=(self.storage.allowlist().get(args.get("recipient_alias"))
                            if args.get("recipient_alias") else None),
            resource_id=resource,
            resource_ref=ref,
        )

    def _validate(self, run_id: str, now: datetime, approval: Optional[Approval]) -> Dict[str, Any]:
        """Admit every step that is both authorized and ready.

        A money-out step is authorized only by an approval bound to *its own*
        symbolic arguments, so approving a plan approves each step it contains,
        and a step that later becomes ready needs no second operator click.
        """
        pending_approval: List[Dict[str, Any]] = []
        denied: List[Dict[str, Any]] = []
        admitted = 0
        for step in self.storage.get_steps(run_id):
            if step["state"] in ("SUCCEEDED", "SKIPPED", "DENIED"):
                continue
            existing = self.storage.conn.execute(
                "SELECT * FROM operations WHERE run_id=? AND step_id=?",
                (run_id, step["step_id"])).fetchone()
            if existing:
                admitted += 1
                continue
            symbolic = self._operation_for_step(run_id, step, now, require_resource=False)
            if symbolic is None:
                continue
            step_approval = (self.storage.get_approval(run_id, symbolic.arguments_hash)
                             if self._bindable(step) else approval)
            if self._bindable(step) and step_approval is None:
                pending_approval.append({"step_id": step["step_id"], "action": step["action"],
                                         "arguments_hash": symbolic.arguments_hash,
                                         "reason": step["reason"],
                                         "amount": symbolic.amount_raw,
                                         "resource_ref": symbolic.resource_ref,
                                         "recipient_alias": symbolic.recipient_alias})
                self.storage.set_step_state(run_id, step["step_id"], "AWAITING_APPROVAL")
                continue
            if not self._dependencies_satisfied(run_id, step):
                self.storage.set_step_state(run_id, step["step_id"], "PENDING")
                continue                 # approved, but its dependency is not verified yet
            operation = self._operation_for_step(run_id, step, now, require_resource=True)
            if operation is None:
                self.storage.set_step_state(run_id, step["step_id"], "PENDING")
                continue
            result = self.executor.admit(operation, step_approval, now)
            if result["decision"] == Decision.DENY:
                denied.append({"step_id": step["step_id"], "action": step["action"],
                               "reason_codes": result["reason_codes"]})
                self.storage.set_step_state(run_id, step["step_id"], "DENIED")
            elif result["decision"] == Decision.REQUIRE_APPROVAL:
                pending_approval.append({"step_id": step["step_id"], "action": step["action"],
                                         "arguments_hash": operation.arguments_hash,
                                         "reason": step["reason"],
                                         "amount": operation.amount_raw,
                                         "recipient_alias": operation.recipient_alias})
                self.storage.set_step_state(run_id, step["step_id"], "AWAITING_APPROVAL")
            else:
                admitted += 1
                self.storage.set_step_state(run_id, step["step_id"], "ADMITTED")

        with self.storage.tx():
            if denied:
                self.storage.set_run_state(run_id, RunState.DENIED,
                                           "policy denied: " + ", ".join(
                                               d["reason_codes"][0] for d in denied), now)
            elif pending_approval:
                self.storage.set_run_state(run_id, RunState.AWAITING_APPROVAL,
                                           "%d step(s) need approval" % len(pending_approval), now)
            else:
                self.storage.set_run_state(run_id, RunState.EXECUTING, "", now)
            append_event(self.storage.conn, run_id, "validation_completed",
                         {"admitted": admitted, "denied": denied,
                          "awaiting_approval": pending_approval}, _iso(now))
        return {"state": ("DENIED" if denied else
                          RunState.AWAITING_APPROVAL if pending_approval else RunState.EXECUTING),
                "denied": denied, "awaiting_approval": pending_approval, "admitted": admitted}

    # --------------------------------------------------------------- approval
    def approve(self, run_id: str, session_id: str = "local-session",
                now: Optional[datetime] = None) -> Dict[str, Any]:
        """One operator action approves the plan: every outstanding money-out step
        gets its own approval row bound to that step's symbolic arguments."""
        now = now or datetime.now(timezone.utc)
        plan = self.storage.get_plan(run_id)
        if plan is None:
            return {"ok": False, "error": "NO_PLAN"}
        approvals: List[Dict[str, Any]] = []
        for step in self.storage.get_steps(run_id):
            if step["state"] not in ("AWAITING_APPROVAL", "PENDING"):
                continue
            if not self._bindable(step):
                continue
            operation = self._operation_for_step(run_id, step, now, require_resource=False)
            if operation is None:
                continue
            if self.storage.get_approval(run_id, operation.arguments_hash):
                continue
            approval = Approval(
                approval_id="apr-" + uuid.uuid4().hex[:12], run_id=run_id,
                plan_hash=plan["plan_hash"], arguments_hash=operation.arguments_hash,
                actor_id=config.LOCAL_ACTOR_ID, approved_at=_iso(now),
                expires_at=_iso(now + timedelta(seconds=config.APPROVAL_TTL_SECONDS)),
                session_id=session_id)
            with self.storage.tx():
                self.storage.create_approval(approval)
                append_event(self.storage.conn, run_id, "step_approved",
                             {"approval_id": approval.approval_id,
                              "step_id": step["step_id"], "action": step["action"],
                              "plan_hash": plan["plan_hash"],
                              "arguments_hash": operation.arguments_hash,
                              "amount": operation.amount_raw,
                              "resource_ref": operation.resource_ref,
                              "expires_at": approval.expires_at,
                              "session_id": session_id}, _iso(now))
            approvals.append({"step_id": step["step_id"], "approval_id": approval.approval_id,
                              "arguments_hash": operation.arguments_hash})
        return {"ok": bool(approvals), "plan_hash": plan["plan_hash"], "approvals": approvals}

    # ---------------------------------------------------------------- advance
    def advance(self, run_id: str, now: Optional[datetime] = None,
                stop_after: Optional[str] = None) -> Dict[str, Any]:
        """One transition. `stop_after` is the demo's deliberate interruption hook."""
        now = now or datetime.now(timezone.utc)
        run = self.storage.get_run(run_id)
        if run is None:
            return {"state": "UNKNOWN_RUN"}

        state = run["state"]
        if state in TERMINAL_RUN_STATES:
            return {"state": state, "note": run["note"]}

        if state == RunState.PLANNING:
            return self._plan(run_id, now)

        if state == RunState.VALIDATING:
            return self._validate(run_id, now, self.storage.get_approval(run_id))

        if state == RunState.AWAITING_APPROVAL:
            approval = self.storage.get_approval(run_id)
            if approval is None:
                return {"state": RunState.AWAITING_APPROVAL, "note": "no approval yet"}
            return self._validate(run_id, now, approval)

        if state in (RunState.EXECUTING, RunState.WAITING_BUYER, RunState.VERIFYING):
            return self._execute(run_id, now, stop_after=stop_after)

        if state == RunState.RECONCILING:
            return self._reconcile(run_id, now)

        return {"state": state}

    # --------------------------------------------------------------- execution
    def _ready(self, run_id: str, step, now: datetime) -> bool:
        operation = self.storage.conn.execute(
            "SELECT * FROM operations WHERE run_id=? AND step_id=?",
            (run_id, step["step_id"])).fetchone()
        return operation is not None and operation["state"] in (
            OpState.RESERVED, OpState.DISPATCHED, OpState.UNKNOWN)

    def _execute(self, run_id: str, now: datetime,
                 stop_after: Optional[str] = None) -> Dict[str, Any]:
        # A dependency that just became verified can unlock a step that was not
        # admissible before (capture needs a captured*order*, refund needs a
        # capture). Re-validate before every execution round.
        validation = self._validate(run_id, now, self.storage.get_approval(run_id))
        run_after_validation = self.storage.get_run(run_id)
        if run_after_validation["state"] in (RunState.DENIED, RunState.FAILED):
            return {"state": run_after_validation["state"],
                    "denied": validation.get("denied", []),
                    "note": run_after_validation["note"]}
        executed: List[Dict[str, Any]] = []
        waiting_buyer = False
        for step in self.storage.get_steps(run_id):
            if step["state"] in ("SUCCEEDED", "DENIED", "SKIPPED"):
                continue
            operation = self.storage.conn.execute(
                "SELECT * FROM operations WHERE run_id=? AND step_id=?",
                (run_id, step["step_id"])).fetchone()
            if operation is None:
                continue
            if operation["state"] == OpState.SUCCEEDED:
                self.storage.set_step_state(run_id, step["step_id"], "SUCCEEDED")
                continue
            if operation["state"] == OpState.FAILED:
                self.storage.set_step_state(run_id, step["step_id"], "FAILED")
                with self.storage.tx():
                    self.storage.set_run_state(run_id, RunState.FAILED,
                                               "step %s failed definitively" % step["step_id"], now)
                return {"state": RunState.FAILED, "executed": executed}

            if not self._dependencies_satisfied(run_id, step):
                waiting_buyer = waiting_buyer or step["action"] == Action.CREATE_ORDER
                continue

            if step["action"] == Action.CAPTURE:
                order = self.storage.conn.execute(
                    "SELECT platform_id FROM operations WHERE run_id=? AND step_id=?",
                    (run_id, step["depends_on"])).fetchone()
                status = None
                if order and order["platform_id"]:
                    status = self.client.get_order(order["platform_id"]).get("status")
                if status != "APPROVED":
                    waiting_buyer = True
                    continue

            result = self.executor.dispatch(operation["operation_id"], now)
            executed.append({"step_id": step["step_id"], "action": step["action"],
                             "state": result.get("state"), "platform_id": result.get("platform_id"),
                             "note": result.get("note")})
            if result.get("state") == OpState.SUCCEEDED:
                self.storage.set_step_state(run_id, step["step_id"], "SUCCEEDED")
                if step["action"] == Action.CREATE_ORDER:
                    waiting_buyer = True
                if stop_after and step["step_id"] == stop_after:
                    with self.storage.tx():
                        self.storage.set_run_state(run_id, RunState.EXECUTING,
                                                   "interrupted after %s" % stop_after, now)
                        append_event(self.storage.conn, run_id, "run_interrupted",
                                     {"after_step": stop_after, "executed": executed}, _iso(now))
                    return {"state": RunState.EXECUTING, "executed": executed,
                            "interrupted": True}
            elif result.get("state") in (OpState.UNKNOWN,):
                with self.storage.tx():
                    self.storage.set_run_state(run_id, RunState.RECONCILING,
                                               "ambiguous outcome on %s" % step["step_id"], now)
                return {"state": RunState.RECONCILING, "executed": executed}

        outstanding = [s for s in self.storage.get_steps(run_id)
                       if s["state"] not in ("SUCCEEDED", "DENIED", "SKIPPED")]
        denied_steps = [s["step_id"] for s in self.storage.get_steps(run_id)
                        if s["state"] == "DENIED"]
        with self.storage.tx():
            if denied_steps:
                self.storage.set_run_state(run_id, RunState.DENIED,
                                           "policy denied: " + ", ".join(denied_steps), now)
                final = RunState.DENIED
            elif not outstanding:
                self.storage.set_run_state(run_id, RunState.SUCCEEDED, "all steps verified", now)
                final = RunState.SUCCEEDED
                append_event(self.storage.conn, run_id, "run_succeeded",
                             {"executed": executed}, _iso(now))
            elif waiting_buyer:
                self.storage.set_run_state(run_id, RunState.WAITING_BUYER,
                                           "waiting for buyer approval in PayPal", now)
                final = RunState.WAITING_BUYER
            else:
                self.storage.set_run_state(run_id, RunState.VERIFYING, "", now)
                final = RunState.VERIFYING
        return {"state": final, "executed": executed, "outstanding": [s["step_id"] for s in outstanding]}

    # ------------------------------------------------------------ reconciliation
    def _reconcile(self, run_id: str, now: datetime) -> Dict[str, Any]:
        outcomes = []
        for operation in self.storage.operations_for_run(run_id):
            if operation["state"] == OpState.UNKNOWN:
                outcomes.append(self.executor.reconcile_operation(operation["operation_id"], now))
        blocked = [o for o in outcomes if o.get("outcome") == "MISMATCH"
                   or (o.get("outcome") == "NOT_FOUND" and not o.get("replay_allowed"))]
        with self.storage.tx():
            if blocked:
                self.storage.set_run_state(run_id, RunState.MANUAL_REVIEW,
                                           "unresolved discrepancy", now)
                final = RunState.MANUAL_REVIEW
            else:
                self.storage.set_run_state(run_id, RunState.EXECUTING, "", now)
                final = RunState.EXECUTING
        return {"state": final, "outcomes": outcomes}

    # ------------------------------------------------------------------ views
    def view(self, run_id: str) -> Dict[str, Any]:
        run = self.storage.get_run(run_id)
        if run is None:
            return {}
        plan = self.storage.get_plan(run_id)
        steps = self.storage.get_steps(run_id)
        operations = self.storage.operations_for_run(run_id)
        events = [dict(r) for r in self.storage.conn.execute(
            "SELECT seq, event_type, payload, at FROM audit_events WHERE run_id=? ORDER BY seq",
            (run_id,))]
        return {
            "run": {"run_id": run["run_id"], "goal": run["goal"], "mode": run["mode"],
                    "state": run["state"], "note": run["note"]},
            "plan": {"version": plan["version"], "plan_hash": plan["plan_hash"]} if plan else None,
            "steps": [{"step_id": s["step_id"], "action": s["action"], "state": s["state"],
                       "reason": s["reason"], "depends_on": s["depends_on"]} for s in steps],
            "operations": [{"operation_id": o["operation_id"], "step_id": o["step_id"],
                            "action": o["action"], "state": o["state"],
                            "amount": o["amount_raw"], "currency": o["currency"],
                            "recipient_alias": o["recipient_alias"],
                            "resource_id": o["resource_id"], "platform_id": o["platform_id"],
                            "attempts": o["attempts"]} for o in operations],
            "events": [{"seq": e["seq"], "type": e["event_type"], "at": e["at"],
                        "payload": e["payload"]} for e in events],
        }
