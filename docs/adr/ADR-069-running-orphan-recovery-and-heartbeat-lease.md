# ADR-069 — `RUNNING`-orphan recovery (reconciler Sweep C) + the DB-side heartbeat lease

**Status:** Accepted
**Date:** 2026-07-01
**Author:** PaynePew (PRD #266, execution-plane durability)
**Amends:** ADR-007 (reconciler gains a third sweep), CONTEXT.md §5 (`heartbeat` primitive), `CODING_STANDARDS.md` Watcher/Worker section (the `RUNNING`-orphan "known gap" becomes an implemented rule)
**Depends on:** ADR-065 (`has_executing_run` forbid-concurrency), ADR-068 (`Job.state` / `settle_job` — the quota accounting a stuck run leaks), ADR-067 (continuation consumer — the terminal event a recovered orphan feeds), ADR-013 amendment / issue #268 (per-action `idempotent` posture)
**Implements:** issues #267 (heartbeat lease), #271 (Sweep C)

---

## Context

The system's source of truth is the `JobRun` row in Postgres; SQS only tracks *message delivery*. The two are coupled solely by the worker's `UPDATE`. SQS's own recovery — visibility-timeout redelivery + DLQ — recovers a **message**, not a **run**.

A worker that hard-crashes (OOM / SIGKILL / host loss) **after** it atomically claims a run (`status='RUNNING'`) but **before** it writes a terminal status leaves the row stuck in `RUNNING`. The redelivered message cannot recover it: the executor claim only accepts `PENDING`/`QUEUED`/`RETRYING` (ADR-007), so the next worker's claim fails and it **deletes the message** as an assumed duplicate — throwing away the only recovery vehicle. The reconciler (issue #30) had **no sweep for `RUNNING`**.

This is not a stranded single run. Under forbid-concurrency (ADR-065), `has_executing_run` counts `PENDING/QUEUED/RUNNING/RETRYING`, so while the orphan sits `RUNNING` the job can never materialize its next recurring tick or trigger-driven downstream — **recurrence and chaining stop permanently**. And `Job.state` never settles (the executor's `settle_job` only fires on a real terminal write), so the job stays `active` forever and **leaks its `active_total` quota slot** — re-introducing the exact lockout ADR-068 fixed. `task.cancel` cannot clear it either (`RUNNING` is deliberately excluded from the cancellable set, ADR-022).

A sweep keyed on `updated_at`/`start_at` age would be wrong: `updated_at` is frozen at claim time, so a legitimately long-running action (a slow HTTP call, a big LLM summarization) would be swept and killed while its worker is alive and well. We need a signal that distinguishes **alive-but-slow** from **dead**.

## Decision

### 1. DB-side heartbeat lease (`job_runs.heartbeat_at`, issue #267)

The claim sets `heartbeat_at = now()` at the `RUNNING` transition. The executor's heartbeat loop — which already extends SQS visibility every ~30s — **also bumps `heartbeat_at`** on the same cadence. The two legs are independent: a failed DB bump does not stop the visibility extension, and a single missed bump just leaves the lease stale until the next successful tick. Unlike `updated_at` (frozen at claim), `heartbeat_at` stays fresh for the life of a legitimately long-running action. It is the observable that separates "alive, still working" from "dead," independent of the action's own timeout.

### 2. Reconciler Sweep C — `RUNNING`-orphan recovery (issue #271)

A third sweep runs each reconciler tick, under `FOR UPDATE SKIP LOCKED` (same multi-instance safety as Sweeps A/B):

```
status = 'RUNNING' AND (heartbeat_at < now() - running_grace OR heartbeat_at IS NULL)
```

`heartbeat_at IS NULL` is included defensively: a `RUNNING` row with no lease cannot be attributed to a live worker (every current claim sets it, so this only catches pathological/legacy rows). The sweep is batch-limited like Sweeps A/B.

Recovery is **keyed on the action's declared idempotency posture** (ADR-013 amendment / issue #268) — data-driven, never a hardcoded action-name check:

- **Non-idempotent** (`email_send`, `slack_post`, `github_digest`, `http_call`): a crashed worker may or may not have performed the external side effect, and the two cases are indistinguishable (two-generals). Blind-retry could double-send. So Sweep C **fails safe**: flip the row to `FAILED`, emit `RunEvent(FAILED, reason='running_orphan')`, call `settle_job` in the same transaction (settles a one-shot/immediate and frees its quota; a no-op for recurring/chained), and log an **operator-visible alert** (`logger.error`). The terminal event flows to the continuation consumer (ADR-067), un-wedging recurrence/chaining.
- **Idempotent** (`echo`, `llm_*`, `calendar_digest_ics`): re-executing is safe, so **recover by re-running**. Reset the row to a **claimable** status (`QUEUED`) with a `RunEvent(REENQUEUED, reason='running_orphan')`, then re-enqueue the SQS message.

### 3. Reset-before-re-enqueue ordering (idempotent path)

The status reset is committed **before** the SQS message is sent. If the message were visible while the row is still `RUNNING`, a fast worker would claim-fail (claim rejects `RUNNING`) and delete the message as a duplicate — **re-orphaning the run**. Sweep C therefore commits the `RUNNING → QUEUED` reset first and sends the message only afterward. A send failure after commit is safe: the row sits `QUEUED` and Sweep B re-enqueues it on a later tick (the backstop).

### 4. `running_grace` calibration

`reconciler_running_grace_seconds` (settings, ADR-010) defaults to **180s**. It MUST exceed a live worker's worst-case lease staleness so a slow-but-alive action is never swept. That floor is `heartbeat_interval(30s) + visibility(60s) = 90s`. We use 2× the floor (180s) to tolerate a couple of missed heartbeat bumps (a transient DB write failure leaves the lease stale until the next tick) plus SQS redelivery jitter, keeping the reconciler the **last responder** behind SQS's own redelivery + DLQ (consistent with ADR-007 / issue #270).

### 5. Claim set is unchanged

The executor claim stays `WHERE status IN ('PENDING','QUEUED','RETRYING')`. Sweep C's reset-before-re-enqueue exists **precisely because** a redelivered message cannot claim a `RUNNING` row. Cancel semantics are unchanged: Sweep C recovers only *dead*-worker orphans (stale lease), never a live `RUNNING` run — that stays best-effort/graceful (ADR-022).

## Consequences

- A worker that dies mid-run no longer wedges its job: recurrence/chaining resume and the `active_total` quota slot frees, restoring the ADR-065 and ADR-068 guarantees under crash.
- A stuck run reaches a truthful terminal state (`failed`) instead of showing `running` forever; the operator gets an auditable `RunEvent` and, for non-idempotent actions, an alert.
- `email_send` is fail-safe **today** without the effectively-once work (PRD #266 S6): a crashed non-idempotent run fails-and-alerts rather than silently double-sending or dropping. The residual "sent, then crashed before the record write" window and the at-least-once vs at-most-once policy are S6's scope, not this ADR's.
- No new run status and no schema change: Sweep C reuses `heartbeat_at` (added in issue #267, migration 0013). The recovery reuses the existing `settle_job` / continuation paths rather than introducing a parallel un-wedge mechanism.

## Alternatives considered

- **Sweep on `updated_at`/`start_at` age.** Rejected: frozen at claim, so it cannot tell a slow-but-alive worker from a dead one — it would kill legitimately long-running actions. The heartbeat lease exists for exactly this.
- **Blind-retry every orphan.** Rejected: double-sends non-idempotent external effects. The idempotency posture gates this.
- **Reset `RUNNING → QUEUED` in the same transaction as (and before) the SQS send inside the sweep.** Rejected: the message could be received and processed before the transaction commits, hitting a still-`RUNNING` row and re-orphaning. Commit-then-send is the safe order.
- **A forceful "terminate in-flight run" op.** Out of scope (PRD #266): cancel stays graceful (ADR-022); Sweep C touches only dead-worker orphans.
