# PayPilot Resolve — Finish the Job, Not Just the API Call

## 1. The wedge

**Build an agent that resolves a changed digital-service order into a verified partial refund and contractor payout—and resumes safely after interruption.**

The unit of value is a resolved business exception, not a generated API request. The agent reads a customer’s scope-change message, matches it to purchased deliverables, proposes a resolution against trusted commercial terms, requests approval, executes permitted actions, and checks their outcomes.

The distinguishing screen is the recovery: **“Refund already completed. Resume with contractor payout; do not refund again.”** Judges see persistent state and observation-driven action, not merely several API calls chained together.

Prize mapping:

- **Best Use of Agentic Commerce:** primary target. Demonstrate a bounded planning, tool-use, observation, and recovery loop.
- **Best Use of PayPal + AI:** primary target. AI interprets the exception; PayPal executes checkout, partial refund, and payout.
- **Best Demo Delivery:** primary target. One understandable story, visible interruption, verified recovery.
- **Most Impactful:** secondary; credible administrative value for small digital-service businesses, without invented adoption claims.
- Main prizes remain a stretch. Do not optimize for Most Creative or unrelated sponsor integrations.

This is **not escrow, split settlement, or production-ready autonomous finance**. The payout is a separate merchant-funded PayPal operation.

## 2. The product

**Target user:** a solo digital-studio owner who sells bundled services and pays contractors, currently reconciling scope changes manually.

**One-sentence pitch:** “PayPilot Resolve turns a customer’s scope change into an approved, recoverable refund-and-contractor workflow, with PayPal evidence for every completed step.”

Use one synthetic job throughout:

- Buyer purchased a UX audit for **$90** and competitor scan for **$30**.
- The **$120** order was approved and captured through sandbox checkout.
- Customer says: “Keep the audit, but drop the competitor comparison.”
- Trusted job records show the scan is unstarted, the audit is delivered, and the approved contractor earns a fixed **$40** for it.
- Resolution: refund **$30**, then pay the approved contractor **$40**, provided that compensation has not already been paid.

Prices, fulfillment status, and recipient identity come from the job record—not the model or customer message.

### Exact 60-second demo narrative

This is the central sequence inside a final video under three minutes. Compress external waits with visible elapsed-time captions, never invented transitions.

| Time | On screen |
|---|---|
| 00–07 | Open the paid job: two deliverables, **$120 captured**, real sandbox capture ID. |
| 07–15 | Paste the customer message and click **Resolve scope change**. |
| 15–24 | Agent activity shows job lookup, capture verification, and message-to-deliverable matching. Evidence highlights “competitor comparison” → “Competitor scan.” |
| 24–32 | Resolution card: **Refund $30; pay contractor $40**. Show fixed compensation, allowlisted recipient, and prerequisite checks. Owner clicks **Approve this plan**. |
| 32–41 | PayPal refund becomes **COMPLETED**. A clearly labeled demo interruption stops the worker before payout. |
| 41–50 | Click **Resume**, then click again. Timeline shows the completed refund being reused and the duplicate resume producing no additional execution. |
| 50–60 | After a labeled wait compression, show verified payout results and the resolution receipt: capture $120, refund $30, remaining sale value $90, contractor payout $40. |

Do not call remaining sale value profit or available PayPal balance.

### Three features that MUST exist

1. **Evidence-grounded resolution planning:** real LLM interpretation, structured tool calls, and another decision after tool observations.
2. **Approved, recoverable execution:** immutable approved plan, deterministic checks, durable checkpoints, duplicate protection, and explicit uncertain states.
3. **Visual resolution receipt:** business outcome, event timeline, and expandable sanitized PayPal evidence.

**Deliberately CUT:** generic chat, arbitrary money commands, multiple currencies, multiple contractors, autonomous negotiation, real inbox integrations, webhooks, production payments, public hosting, multi-user authentication, and sponsor-tool shopping. Disable the legacy direct-execution route so it cannot bypass approval.

## 3. Scope for a 2-4 day focused build

Build vertically. Preserve the working sandbox checkout rather than replacing it.

### First half-day: establish feasibility

Confirm official eligibility for a China-based solo entrant, permitted existing work, submission requirements, and prize-receipt restrictions using existing accounts.

Verify that the existing LLM endpoint can be used without cash charges or paid overages. Run one genuine structured decision. If unavailable, test a no-signup local model immediately; do not disguise rules as AI.

**Done:** real model output, sandbox read access, Python 3.9 compatibility, and no unresolved account or spending dependency. If no genuine zero-cost AI path works, stop the redesign.

### Day 1: job state and verified tools

Add SQLite tables for jobs, plans, operations, and events. Seed one synthetic job with authoritative prices, fulfillment flags, and a recipient alias resolved from local configuration. Associate checkout with that local job.

Extend the existing client with the reads needed to verify capture, refund, and payout outcomes. Make mock mode stateful, with local simulated checkout rather than a fake PayPal approval link.

**Done:** the same job can complete checkout locally in mock mode or through real sandbox approval; restarting Flask preserves its state.

### Day 2: agent and approval boundary

Expose a small JSON tool protocol: read job, inspect payment, propose resolution, execute approved step, inspect result. Use multiple LLM turns with bounded iterations and timeouts.

The model selects deliverables and next actions. Deterministic code computes amounts, validates currency and refundable balance, resolves the approved recipient, and enforces prerequisites. Bind human approval to the exact stored plan; changes require reapproval.

**Done:** the real model resolves the example and reacts to tool results. Missing evidence, unsupported requests, malformed output, or attempted recipient changes cannot trigger writes.

### Day 3: recovery and failure tests

Persist operation identity and intended payload before each write. Use stable, endpoint-appropriate idempotency identifiers, including payout batch identifiers. Reconcile uncertain outcomes before further writes; block for review when they cannot be established.

Treat payout batch status separately from item status. Do not label the contractor paid merely because a batch was accepted.

**Done:** restart after the refund checkpoint and double-resume do not repeat completed operations. Do not claim universal “exactly once” execution.

### Day 4, or remaining time: presentation and packaging

Build a single-screen job workspace using local CSS and JavaScript. Add loopback binding, request-token protection, persistent sandbox/mock badges, and readable failure states.

**Done:** record the complete sandbox sequence, run the actual test suite, update the MIT repository and setup instructions, and produce the public English video and project story. Target at least 30 focused offline tests; report only the count actually passing.

## 4. Risk analysis

### 1. Judges still see a dressed-up workflow script

This remains a crowded category. Additional steps do not automatically make an agent.

**Cheapest credible mitigation:** show live model-selected tool calls and a second decision based on observed state. Include a paraphrased request and an unsupported request in evaluation. Explain the boundary honestly: AI interprets and chooses; deterministic controls authorize. Lead with interrupted exception resolution, not “chat with PayPal.”

### 2. Complexity damages the functional demo

Sandbox delays, ambiguous writes, model variability, and recovery bugs can consume the entire build window.

**Cheapest credible mitigation:** one currency, one job, two deliverables, one contractor, capped agent turns. Implement persistence before animation. Freeze features after Day 3. Label the interruption as intentional fault injection and preserve actual external statuses. If a result remains pending, show pending—not success.

### 3. Local-only delivery prevents judges from seeing the value

A clone-and-configure requirement creates friction; an attractive video alone may look staged.

**Cheapest credible mitigation:** lead the README with a short animated recovery sequence, the public video, a sanitized evidence receipt, and a credential-free mock walkthrough. Clearly distinguish mock reproduction from sandbox evidence. Resolve administrative eligibility before investing further.

**My revised probability of any cash prize: approximately 20%, conditional on eligibility and polished completion.** This is subjective, not a statistical estimate. The redesign creates credible specialist-prize arguments and a memorable demo, roughly doubling the v1 estimate, but offers neither established user traction nor a radically new category. Main-prize odds remain low.

## 5. Evidence plan

The final submission must prove behavior, not merely describe architecture.

- **Real money-operation chain:** show sandbox checkout approval, associated order and capture IDs, capture **COMPLETED**, partial refund ID and **COMPLETED**, payout batch ID, and individual payout-item outcome. Include sanitized read responses establishing the final states.
- **Real AI:** record model name, structured tool selections, evidence references, and subsequent tool observations. Display `LLM`, `rules`, and `mock` sources accurately. Rules fallback is availability support, not proof of AI integration.
- **Recovery:** retain a local event sequence spanning the labeled interruption and restart. The same refund resource must appear before and after recovery. Record application invocation counts and stable operation identifiers; pair these with provider records rather than treating local logs alone as proof.
- **Tests:** publish actual pytest output and commit SHA. Cover duplicate approval, repeated checkout return, double resume, process restart, unknown provider outcome, over-refund, currency mismatch, unapproved recipient, malformed model output, and instructions embedded in untrusted customer text.
- **Visual artifacts:** approved plan screenshot, recovery timeline, final receipt, and under-three-minute public running-demo video. Use English narration and captions.
- **Privacy:** commit only reviewed, sanitized artifacts. Exclude `.env`, SQLite databases, tokens, actual recipient addresses, payer details, and raw sensitive logs. Retain sandbox resource IDs where safe for verification.
- **Reproducibility:** document macOS/Python 3.9 setup, mock execution, existing-account sandbox configuration, and limitations. Link the submitted release from Devpost.

No fabricated testimonials, benchmark savings, success statuses, or production-readiness claims.

## 6. Reject list

- **General-purpose PayPal copilot:** preserves v1’s weakest position—broad commands, shallow differentiation, unnecessary financial authority.
- **Autonomous shopping agent:** requires convincing discovery, sellers, purchasing permissions, and checkout integrations; too much unverified surface for four days.
- **AI dispute defender:** compelling stakes, but realistic evidence and dispute lifecycle access are harder to establish than partial-refund execution.
- **Marketplace escrow or milestone release:** risks misrepresenting ordinary checkout and payouts as escrow or protected fund custody.
- **Sponsor-tool integration stack:** adds accounts, dependencies, and presentation overhead without improving the central result. Pursue no tool prize unless an already-available integration directly strengthens this workflow.