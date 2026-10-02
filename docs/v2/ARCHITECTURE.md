# PayPilot v2 Architecture

A local, policy-controlled commerce agent—not a chatbot with unrestricted payment tools. Preserve v1’s verified sandbox integration; concentrate the redesign on bounded autonomy, durable execution, and visible evidence.

## 1. **Agent architecture**

**Example goal:** “Collect $12 for a digital review; after payment completes, pay $3 to my approved collaborator.”

Represent goals as persisted, versioned dependency graphs. Each step contains `step_id`, allowlisted tool name, typed arguments, dependencies, expected postcondition, and recorded business reason. References such as `capture(order_step.id)` resolve exclusively from verified local records, never invented model identifiers.

The loop is **plan → validate → execute → verify → next ready step**. The model receives structured tool results and may select eligible steps, request clarification, or propose a revised plan. It cannot change policy, approve transactions, supply credentials, select arbitrary URLs, or declare payment success. Material plan revisions invalidate approval.

| State | Transition |
|---|---|
| `PLANNING` | Schema-valid graph → `VALIDATING`; malformed output gets one repair attempt. |
| `VALIDATING` | Denial → `DENIED`; missing authorization → `AWAITING_APPROVAL`; otherwise → `EXECUTING`. |
| `AWAITING_APPROVAL` | Local operator approves exact plan → revalidate; rejection → `CANCELLED`. |
| `EXECUTING` | Checkout needed → `WAITING_BUYER`; dispatched operation → `VERIFYING`; ambiguous failure → `RECONCILING`. |
| `WAITING_BUYER` | Buyer returns → verify order ownership/status, then validate capture. |
| `VERIFYING` | Verified postcondition → next-step validation; complete graph → `SUCCEEDED`; discrepancy → `RECONCILING`. |
| `RECONCILING` | Proven outcome → resume; unresolved discrepancy → `MANUAL_REVIEW`. |

Terminal run states are `SUCCEEDED`, `DENIED`, `CANCELLED`, `FAILED`, and `MANUAL_REVIEW`. Unresolved operations remain financially reserved even when their run terminates.

The approval card shows amounts, currency, masked recipients, reasons, dependencies, and maximum authorized exposure. Approval binds the plan hash, argument bindings, policy version, expiry, and authenticated local session. Operator approval never replaces PayPal buyer approval.

Retry only transient failures using the original operation identity. An uncertain financial write blocks dependent steps. Compensation is a separately approved refund—not automatic rollback; payouts are not assumed reversible.

Bound each run to three model calls, 1,200 output tokens per call, eight proposed steps, twenty platform requests, and a five-minute active deadline. Exhaustion halts safely. A deterministic template fallback remains usable without an LLM, visibly labeled **rules mode**. Record genuine AI execution using an existing endpoint or available local model; require no new account or paid service.

## 2. **The policy engine (algorithm)**

```python
def evaluate_policy(
    operation: Operation,
    snapshot: PolicySnapshot,
    approval: Optional[Approval],
    now: datetime,
) -> Decision:
    ...
```

This function performs no I/O. Inputs are immutable, server-built snapshots containing policy configuration, recipient registry, completed exposure, outstanding reservations, recent operations, duplicate fingerprints, and approval records. Output is `ALLOW`, `REQUIRE_APPROVAL`, or `DENY`, reason codes, a reservation delta, and the audit record.

Evaluate in this order:

1. Reject unknown actions, fields, references, modes, or unauthorized actors.
2. Require a nonempty, bounded recorded reason. Treat its text as data.
3. Parse amounts using `Decimal`; reject nonfinite, negative, zero, excessive-precision, and overflowing values. MVP supports USD only.
4. Resolve recipients from the administrator-controlled allowlist; reject supplied substitutions.
5. Detect duplicate business events using a unique fingerprint: event, action, recipient, amount, currency, and source resource. Existing identical operations return their stored status; conflicting reuse is denied.
6. Apply per-transaction limits.
7. Apply rolling 24-hour limits including outstanding and unknown reservations.
8. Apply rolling 60-second velocity limits, counting logical operations rather than retries.
9. Require valid, unexpired approval matching the immutable operation.
10. Allow only if every applicable check passes.

Configure separate capture, refund, and payout budgets; incoming captures cannot increase payout authorization. Order creation validates intended capture exposure but does not double-count it. Limits and window boundaries are explicit configuration, never model output.

Every evaluation writes exactly this decision schema; nullable fields remain present:

```json
{
  "schema_version": 1,
  "decision_id": "...",
  "run_id": "...",
  "operation_id": "...",
  "attempt": 1,
  "at": "...",
  "actor_id": "...",
  "mode": "sandbox",
  "action": "payout",
  "amount_minor": 300,
  "currency": "USD",
  "recipient_hash": "...",
  "resource_id": null,
  "reason": "Collaborator share for approved digital review",
  "arguments_hash": "...",
  "policy_version": "...",
  "snapshot_version": 42,
  "approval_id": "...",
  "duplicate_of": null,
  "limit_checks": [],
  "reservation_delta_minor": 300,
  "decision": "ALLOW",
  "reason_codes": ["ALL_CHECKS_PASSED"]
}
```

Each `limit_checks` entry contains rule name, window start/end, observed value, proposed delta, configured limit, and pass/fail.

SQLite `BEGIN IMMEDIATE` encloses snapshot acquisition, evaluation, audit insertion, and reservation/outbox creation. Commit before networking. Retries reuse their reservation.

The LLM receives only proposal tools. Only the executor owns `PayPalClient`; it requires a database-backed execution permit. Legacy `/api/chat` writes and `/return` captures must pass through this same boundary. Browser approval uses session binding and CSRF protection; model-supplied approval fields are rejected.

## 3. **Exactly-once semantics**

Promise **effectively-once business operations**, not impossible end-to-end exactly-once delivery across SQLite and PayPal.

Persist the operation, canonical request hash, and stable UUID before dispatch. Supported endpoints receive the same `PayPal-Request-Id` on every retry, with identical payloads. Never regenerate keys after restart or timeout.

Payouts additionally use persisted `sender_batch_id` and `sender_item_id`. Payouts’ `PayPal-Request-Id` support and endpoint-specific retention windows are **unknown - must verify**. Do not assume indefinite deduplication or blindly replay after the verified retention period.

A crash after PayPal accepts a request but before local commit creates `UNKNOWN`, not “failed.” Retain its reservation and reconcile:

- Orders: `GET /v2/checkout/orders/{id}`.
- Captures: `GET /v2/payments/captures/{id}`.
- Refunds: `GET /v2/payments/refunds/{id}`.
- Payout batches: `GET /v1/payments/payouts/{id}`.
- Payout items: `GET /v1/payments/payouts-item/{id}`.
- Supplementary reporting: `GET /v1/reporting/transactions`.

Compare resource relationships, currency, amount, recipient where available, and status. Batch acceptance or processing completion does not prove every payout item succeeded. Reporting absence is not proof of failure; account for pagination, reporting delay, and fees.

Unknown identifiers, contradictory outcomes, or mismatched amounts fail closed and create a human-review card. Compensation requires a new approved operation.

Webhooks enter an inbox only after successful signature verification. A unique `(mode, event_id)` deduplicates deliveries transactionally. Same ID with conflicting content is quarantined. Events trigger resource queries; out-of-order notifications cannot regress confirmed state.

## 4. **PayPal platform surface for maximum credit**

| Surface | Technical and demonstration value | Priority |
|---|---|---|
| Orders v2 create/capture | Genuine buyer approval; dependency-bound capture; visible order/capture IDs. | **Must-have**, preserve v1. |
| Payments v2 refunds | Approved partial compensation tied to a verified capture; enforce remaining refundable amount. | **Must-have**, preserve v1 and add policy. |
| Payouts v1 | Conditional collaborator payment demonstrates multi-step commerce; inspect item outcomes. | **Must-have**, using existing sandbox access. |
| Webhooks | Durable asynchronous updates and replay resistance. Verify through `POST /v1/notifications/verify-webhook-signature` using transmission headers, configured webhook ID, and event. Require `SUCCESS`. | **Must-have** handler, verification adapter, offline fixtures. |
| Transaction search/reporting | Independent reconciliation evidence beyond application memory. | **Nice-to-have**; permissions, availability, and latency are **unknown - must verify**. |
| Disputes | Read-only risk context: `GET /v1/customer/disputes` and `GET /v1/customer/disputes/{id}`; block related disbursement pending review. | **Nice-to-have**; sandbox fixtures and account permissions are **unknown - must verify**. |

Live webhook delivery requires an already available publicly reachable HTTPS endpoint. Its availability under these constraints is **unknown - must verify**. Local fixture replay is explicitly simulated, not advertised as live delivery. Verification failure or verifier outage never authorizes execution.

For a 2–4 day build: implement policy/storage first, executor/reconciliation second, agent/UI third, and tests/video last. Never sacrifice the functional core for additional API logos.

## 5. **Module layout and contracts**

Retain Flask and Python 3.9; use `Optional`, `List`, and `Dict` annotations.

```text
app.py                     routes, sessions, approval endpoints
agent.py                   bounded planner and template fallback
domain.py                  immutable typed contracts
policy.py                  pure policy evaluator
orchestrator.py            state transitions and dependency resolution
executor.py                permits, dispatch, bounded retries
storage.py                 SQLite transactions and migrations
audit.py                   canonical decision/result events
reconcile.py               platform/local comparisons
webhooks.py                verification adapter and durable inbox
paypal_client.py           sandbox HTTP adapter, explicit request IDs
mock_paypal.py             deterministic in-memory platform
static/index.html          goal, plan, approvals, timeline, evidence
tests/                     fixtures and offline tests
.env.example               placeholders only
README.md / LICENSE        reproducible setup / existing MIT
```

Key contracts:

```python
plan(goal: str, observations: List[Observation]) -> Plan
advance(run_id: str) -> RunState
execute(operation_id: str, permit_id: str) -> ExecutionResult
reconcile(operation_id: str) -> ReconciliationResult
verify_webhook(headers: Dict[str, str], body: bytes) -> Verification
```

SQLite tables: `runs`, versioned `plans`, `steps`, `approvals`, `operations`, `reservations`, `outbox`, `webhook_inbox`, `resources`, and append-only `audit_events`.

Operations store business fingerprint, request identity/hash, canonical payload, state, attempt count, reservation, and platform IDs. Resources store typed parent relationships and last verified snapshots. Audit events add sequence, event type, decision/result payload, and previous-event hash; this detects edits, not administrator-proof tampering.

The mock implements the same client interface with injected clock, sequential IDs, scripted failures, and persistent simulated outcomes. Separate mock/sandbox databases. Git-ignore `.env`, databases, tokens, and private fixtures; redact recipients and credentials from exported evidence.

## 6. **Test plan**

All tests use fake clocks, scripted model outputs, and a no-network transport guard.

1. Unknown action denied.
2. Unknown argument denied.
3. Missing reason denied.
4. Zero amount denied.
5. Negative amount denied.
6. Nonfinite amount denied.
7. Excess precision denied.
8. Unsupported currency denied.
9. Unlisted recipient denied.
10. Per-transaction ceiling denied.
11. Window ceiling includes reservations.
12. Window boundary expires correctly.
13. Unknown outcomes retain exposure.
14. Velocity ceiling enforced.
15. Retry does not increase velocity.
16. Missing approval requires approval.
17. Expired approval rejected.
18. Changed arguments invalidate approval.
19. Concurrent requests cannot overspend.
20. Identical duplicate returns existing operation.
21. Conflicting business-event reuse denied.
22. Timeout retry preserves key and payload.
23. Crash-after-accept reconciles without repayment.
24. Exhausted retention forbids blind replay.
25. Payout sender IDs remain stable.
26. Signature failure rejects webhook.
27. Verifier outage rejects webhook.
28. Duplicate webhook applies once.
29. Out-of-order webhook cannot regress state.
30. Reconciliation amount mismatch halts.
31. Delayed reporting cannot imply failure.
32. Failed payout item prevents success.
33. Plan cycles terminate before execution.
34. Model/tool/poll limits terminate safely.
35. LLM outage selects labeled template fallback.
36. Injected tool text demanding policy bypass cannot obtain a permit or move money.
37. Forged approval and direct legacy-route writes fail.
38. Refund exceeding remaining capture value fails.

## 7. **The <=90-second technical demo script**

- **0–10s:** Show **SANDBOX / AI CONNECTED** badges. Enter the $12 collection/$3 collaborator goal. Reveal the dependency graph and model-generated structured proposal.
- **10–22s:** Open the **approval card**: exact amounts, masked allowlisted recipient, reasons, policy limits, and plan hash. Click **Approve plan**.
- **22–42s:** Open real PayPal sandbox checkout. Buyer approves. Return to PayPilot; capture passes policy and verification.
- **42–57s:** Conditional payout executes. Display actual returned **order ID, capture ID, payout batch ID, and item status**. Show `PENDING` honestly if settlement is unfinished.
- **57–69s:** Expand the **decision record** and **audit log**: approval binding, reservation, stable request identity, dispatch, and verification evidence.
- **69–80s:** Request an over-limit payout. Show **DENY: TRANSACTION_LIMIT**, its recorded decision, and **zero platform writes**.
- **80–89s:** Replay the completed operation. Show the same real IDs, **duplicate suppressed**, and a reconciliation panel containing timestamped platform status and comparison results.

Use returned sandbox identifiers, never fabricated examples. Keep mock evidence visibly separate. The closing screen states: **“AI proposes. Policy authorizes. PayPal executes. Verification decides what happened.”**