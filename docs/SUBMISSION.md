## Inspiration

Every "AI + payments" demo I had seen ended at the same place: a sentence turns into an API call, and everybody applauds. That is the easy part. The hard part, the part a business actually pays for, is **finishing the job** — including when the job changes halfway through.

So PayPilot Resolve takes a different unit of work: **a business exception**. A buyer ordered a UX audit *and* a competitor scan, paid for both, and then dropped the scan. Now someone has to refund the dropped part *and* pay the contractor who delivered the rest — without refunding twice, without paying twice, and without losing the thread if the process dies in the middle. That is what this agent does, and the whole design exists to make it safe.

## What it does

One sentence in. A versioned plan out:

```
Collect $120 for the delivered audit and competitor scan.
The client dropped the competitor scan, so refund $30 and pay
$40 to the approved collaborator.
```

The planner (an LLM, with a labelled deterministic fallback) proposes a four-step dependency graph — collect, capture, refund, pay — and every step carries a business reason. Nothing moves yet.

Then:

1. A **policy engine** evaluates each step deny-by-default: unknown actions, zero or over-precise amounts, unsupported currency, unlisted recipients, per-transaction ceilings, rolling 24h ceilings (including exposure from *unknown* outcomes), velocity, and an unexpired approval.
2. The operator approves the plan **once**. That approval binds each money-out step to its exact arguments: amount, currency, recipient alias, symbolic resource reference and reason.
3. Only then does the executor touch PayPal: a real sandbox order, the buyer approving in the real checkout, PayPal redirecting back, the server-side capture, the partial refund, the collaborator payout.
4. **Verification decides what happened.** The console shows the platform ids PayPal returned, not a green checkmark the app invented.

## How I built it

Two layers and a hard boundary between them.

**The planner proposes, it never authorizes.** It emits a structured graph; everything it says is validated before it can become an operation — unknown tools, dangling dependencies, missing reasons, and any raw payment address are rejected, and a reference to a dependency is resolved *by role* (a refund can only ever target a capture, a capture only an order) rather than by whatever field name the model wrote.

**The executor is the only component that owns the PayPal client**, and it requires a database-backed admission: a persisted operation, a reservation, and a decision record — all committed in one `BEGIN IMMEDIATE` transaction before any network call.

Around that: SQLite state, a hash-chained audit log, idempotency via a persisted `PayPal-Request-Id` (plus `sender_batch_id` / `sender_item_id` for payouts), and a reconciliation path that reads the platform instead of guessing. An ambiguous outcome becomes `UNKNOWN` with its reservation retained, never a silent failure.

## Challenges

The genuinely hard parts were not the ones I expected.

- **A crash between "PayPal accepted" and "we committed"** is the interesting failure. The design answer is to commit intent first, keep the reservation, and reconcile by reading the platform — and to refuse a blind replay once the request-id retention window is plausibly over.
- **Letting the model be creative about what and boring about how.** The model kept trying to supply a capture amount and to point a refund at an order id. Both are now normalised server-side, and the substitution is written into the audit log rather than hidden.
- **Proving the agentic part is real.** An interruption mid-run, followed by a resume that reuses the completed refund and pays the contractor exactly once, is the part that separates this from a workflow script — so it is in the demo.

## Accomplishments

A real sandbox run from the recorded demo, read back from PayPal afterwards: order `95H90132D52748058` `COMPLETED`, capture `6UT72402FC133551Y` `PARTIALLY_REFUNDED`, refund `7E550592AE673915F` `COMPLETED`, payout `MGPPKAXY53DVJ` batch `SUCCESS`. 62 offline tests cover the policy engine, exactly-once behaviour, webhook intake and the state machine, with no credentials and no network.

## What I learned

For payments, the interesting question is never "can the model call the API" — it is "what stops it". Deny-by-default, approvals bound to exact arguments, reservations that survive a crash, and reconciliation over guessing are what make an agent something a business could actually run.

## What's next

- live webhook delivery (the handler and verification adapter exist; they need a reachable endpoint)
- an approval card that shows the intent and the amount side by side, with a diff when the plan changes
- partial-approval: allowing an operator to approve three of four steps
