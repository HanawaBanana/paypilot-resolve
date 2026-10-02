# PayPilot Resolve

**An agent that resolves a changed order into a verified refund _and_ a contractor
payout — and resumes safely if it is interrupted.**

This is not a chatbot with an unlimited payments tool. It is a small,
policy-controlled commerce agent built for the *PayPal AI Hackathon*: the model
proposes a plan, a deterministic policy engine authorizes it, an operator
approves it once, PayPal executes it, and verification decides what actually
happened.

> AI proposes. Policy authorizes. PayPal executes. Verification decides what happened.

**Demo video (2:20, public): <https://youtu.be/Rv2cLNXboPw>** — a real PayPal sandbox
run, start to finish, including the interruption and the resume.

Real evidence from the recorded run (read back from PayPal, not from local state):

| resource | id | platform status |
|---|---|---|
| order | `95H90132D52748058` | `COMPLETED` |
| capture | `6UT72402FC133551Y` | `PARTIALLY_REFUNDED` |
| refund | `7E550592AE673915F` | `COMPLETED` |
| payout | `MGPPKAXY53DVJ` | batch `SUCCESS` |

62 offline tests pass with no credentials and no network (`python -m pytest -q`).

---

## Why this exists (the wedge)

"Turn a sentence into an API call" is the baseline every hackathon entry ships.
The valuable unit here is different: **resolving a business exception**. A buyer
changes their mind about part of an order; the operator has to refund the part
that was dropped *and* pay the contractor who did the delivered part — without
paying anyone twice, and without losing the thread if the process dies in the
middle.

PayPilot Resolve makes that whole resolution auditable: a versioned plan, an
approval bound to exact arguments, reservations that survive a crash, and a
settlement receipt that cites real PayPal ids.

## The safety property (what the demo proves)

- **The model cannot move money.** It emits proposals (`step_id`, allowlisted
  tool, typed arguments, dependency, expected postcondition, business reason).
  Only the executor owns the PayPal client, and only with a database-backed
  execution permit.
- **Deny by default.** Every write passes a pure policy function first: unknown
  actions/fields, missing reason, zero/negative/non-finite/over-precise amounts,
  unsupported currency, unlisted recipients, per-transaction ceilings, rolling
  24h ceilings (including *unknown* exposure), velocity ceilings, and an
  unexpired approval that binds the exact argument hash.
- **Effectively-once.** The operation, its canonical payload hash and a stable
  request id are committed before any network call; retries reuse them. A crash
  after PayPal accepts but before we commit leaves an `UNKNOWN` reservation —
  never a silent "failed" — and reconciliation decides.
- **Fail closed.** Unknown ids, contradictory outcomes and mismatched amounts
  halt and create a human-review card instead of guessing.

See [`docs/v2/ARCHITECTURE.md`](docs/v2/ARCHITECTURE.md) for the full design
(state machine, policy evaluation order, decision-record schema, test plan) and
[`docs/v2/CONCEPT.md`](docs/v2/CONCEPT.md) for the product reasoning.

## Quick start (no credentials, no network)

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env                 # PAYPILOT_MODE=mock
.venv/bin/python -m pytest -q        # deterministic, offline
```

`mock` mode is a stateful in-memory PayPal: the same client interface, injected

`.venv/bin/python -m pytest -q        # 62 deterministic tests, no network
checkout, capture, partial refund, conditional payout, interruption and resume —
runs end to end with zero credentials, which is also how the tests stay
deterministic.

## Running against the real PayPal sandbox

```env
PAYPILOT_MODE=sandbox
PAYPAL_CLIENT_ID=...            # sandbox app, developer.paypal.com
PAYPAL_CLIENT_SECRET=...
```

Sandbox credentials never belong in the repository. `recipients.json` ships with
placeholders; point `PAYPILOT_RECIPIENTS` at a local file for real addresses —
the operator alias, not the address, is what the plan and the audit log carry.

## Layout

```
domain.py         immutable contracts: Operation, Approval, PolicySnapshot, Decision
policy.py         the pure policy evaluator (no I/O, fully unit-tested)
config.py         every limit and window — administrator-controlled, never model output
audit.py          append-only, hash-chained audit log
storage.py        SQLite transactions, reservations, outbox
executor.py       execution permits, dispatch, bounded retries
orchestrator.py   state machine and dependency resolution
agent.py          bounded planner (model proposals) + labelled rules fallback
reconcile.py      local vs platform comparison
webhooks.py       signature verification + durable inbox
paypal_client.py  PayPal sandbox REST adapter with explicit request ids
mock_paypal.py    deterministic local platform
static/index.html goal, plan, approval card, timeline, evidence
tests/            offline tests (policy, idempotency, webhook, reconciliation)
```

## Honest limitations

- USD only. One operator. Local-only: there is no hosted instance, so the demo
  video and the mock walkthrough are the reproducible evidence.
- Live webhook delivery needs a publicly reachable HTTPS endpoint; the handler
  and verification adapter are implemented and covered by offline fixtures.
- Payout `PayPal-Request-Id` support and its retention window are *not assumed*:
  payouts additionally carry persisted `sender_batch_id` / `sender_item_id`, and
  reconciliation — not blind replay — resolves an unknown outcome.
- The audit log is hash-chained: it detects edits, it is not tamper-proof
  against an administrator with database access.

## License

MIT — see [`LICENSE`](LICENSE).
