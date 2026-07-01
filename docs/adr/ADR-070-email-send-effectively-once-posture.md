# ADR-070 — `email_send` effectively-once: run-derived idempotency key + write-ahead intent

**Status:** Accepted
**Date:** 2026-07-01
**Author:** PaynePew
**Depends on:** ADR-008 (SQS at-least-once delivery), ADR-013 + issue #268 (per-action `idempotent` posture), ADR-045 / ADR-050 (email_send design, Gmail-only), ADR-067 (continuation), PRD #266 (execution-plane durability)
**Amends:** `CODING_STANDARDS.md` (action-registry section), CONTEXT.md §8

---

## Context

`email_send` is a **non-idempotent external side effect** (`idempotent = False`, ADR-013/#268): calling it twice sends two real emails. Our transport gives **at-least-once delivery**, not exactly-once:

- SQS redelivers a message whose visibility expired (a slow or crashed worker).
- A worker can crash mid-`RUNNING` **after** the Gmail send returns 200 but **before** it writes the terminal `SUCCEEDED` — and we cannot distinguish that from "crashed *before* the send" (two-generals).
- The executor's retryable path (`RETRYING`) re-executes the handler on redelivery; a network error while *reading* the Gmail response looks identical to a network error before the request arrived.

Before this ADR, `email_send` carried no idempotency key and no dedup record, so any of the above re-executes the send and delivers a duplicate. Exactly-once end-to-end for an external side effect is **impossible in principle** — so the honest goal is **effectively-once**: at-least-once delivery + an idempotent consumer at our boundary, with the residual duplicate window made explicit and bounded rather than hidden.

## Decision

### 1. Run-derived idempotency key
Each logical send gets a stable key derived only from the action name and `run_id`: `derive_idempotency_key(action, run_id) → f"{action}:{run_id}"` (`app.actions.send_dedup`). Every replay of the same run computes the same key — the property the whole scheme rests on. `run_id` is the right scope: a redelivery/reconciler-retry reuses the same run, while a legitimately distinct send (a new recurring tick, a new chained downstream) is a new run and a new key.

### 2. Write-ahead intent + app-side dedup record
A durable `send_intents` row (migration 0014), **primary-keyed on the idempotency key** so the write-ahead insert is atomic:

- **Before** the Gmail call, `PostgresDedupStore.begin(key, run_id)` does `INSERT ... ON CONFLICT DO NOTHING`. The inserter wins and gets `send`; a conflict reads the existing row.
- **After** a confirmed 2xx, `mark_sent(key, provider_message_id)` flips the row to `sent` and stores the Gmail message id — **in its own committed transaction, before the handler returns**, so the "sent" fact survives even if the executor's terminal write then fails and the message redelivers.
- The pure decision (`decide_send`) maps the stored status → outcome: `None → send`, `sent → skip` (dedup hit, **no** Gmail call), `attempting → resend`.

### 3. Delivery-bias policy (the residual window)
Exactly one window remains: **"sent, then crashed before the `sent` write."** A replay then reads `attempting` and cannot tell whether the provider actually sent. The chosen posture:

- **`email_send` biases to at-least-once and tolerates an extremely rare duplicate** — `attempting` → `resend`. Losing important mail is worse than a rare double.
- A future **high-stakes** flag flips a specific send to **at-most-once + alert** — `attempting` → fail-and-alert (never a silent second send). Not implemented in this slice; the `resend` vs fail-and-alert branch is where it will hook in.

### 4. Interaction with the reconciler
This is the app-side complement to the reconciler's `RUNNING`-orphan recovery (PRD #266 S3). Because `email_send` is `idempotent = False`, an orphaned `RUNNING` email run **fails-and-alerts** (it is not blind-retried), so the two mechanisms never fight. The dedup record additionally protects the one path that *does* re-execute the same run: SQS redelivery of a `RETRYING` run.

## Consequences

- A redelivered or retried `email_send` for a run already confirmed `sent` is a **no-op success** (`result.deduped = True`, same envelope shape, echoing the stored `provider_message_id`) — no duplicate email.
- The provider message id is now captured on success (previously discarded).
- One new table (`send_intents`); no status-machine change (CONTEXT.md §2 unchanged — `RUNNING` and the 7 run statuses are untouched).
- The guarantee is **effectively-once, not exactly-once**; the residual duplicate window is documented, bounded to a single instant, and biased toward delivery. This is the interview-defensible claim (PRD #266 user story 14): "at-least-once delivery + idempotent consumer = effectively-once; exactly-once end-to-end for an external side effect is impossible."

## Alternatives Considered

| Option | Reason rejected |
|---|---|
| Provider-side idempotency (transactional-email API with a native idempotency key) | A provider swap is a separate, larger design (PRD #266 out-of-scope). App-side dedup works with Gmail today. |
| Dedup keyed on message content (to+subject+body hash) | Two legitimately-identical emails (e.g. the same daily digest two days running) would collide and the second be dropped. `run_id` scopes dedup to *this* delivery, not *this content*. |
| Mark `sent` in the same transaction as the executor's terminal write | The handler returns an `ActionResult` to the executor, which owns the terminal write; coupling the dedup write into that transaction would leak the send seam into the worker and still not close the "crashed after send" window. A dedicated write-ahead + mark-sent, committed inside the handler, is tighter. |
| Do nothing (rely on SQS + claim-and-mark) | Claim-and-mark blocks a *concurrent* re-claim of a `RUNNING` row but not a *sequential* `RETRYING` redelivery, which re-executes the send. That is the exact double-send this closes. |
