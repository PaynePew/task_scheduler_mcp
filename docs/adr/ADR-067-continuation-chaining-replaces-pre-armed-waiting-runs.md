# ADR-067 — Continuation chaining: terminal-event run creation replaces pre-armed `WAITING` runs

**Status:** Accepted
**Date:** 2026-06-30
**Author:** PaynePew (design session, grill-with-docs)
**Amends:** ADR-065 (reverses its pre-arm control plane + `WAITING` run state), ADR-064 (folds the `from_run_id` data plane into creation), ADR-033 (revises the "ChainWatcher must never create runs" constraint that drove ADR-065's rejection of this model), CONTEXT.md §2 + §7
**Validated by:** a job-scheduler survey (deep-research, 2026-06-30) + the project's own reference design `design-chatgpt-tasks.pdf` + a throwaway state-machine prototype (scenarios A–H, 8/8 — continuation, exactly-once, slow-consumer skip, terminal-race CAS)

---

## Context

The **chaining control plane** — pre-armed `WAITING` runs, `ChainWatcher` flipping them to `PENDING`/`CANCELLED`, recursive `arm`, and a flip-time slow-consumer drop (ADR-065) — is the most complex and least-precedented part of the system, and the source of its hardest bugs (the #227 → #234-237 arm-race saga).

Two independent inputs converged on a simpler model:

1. **The project's own reference design** (`design-chatgpt-tasks.pdf`, the system-design exercise this repo implements) builds recurrence as **continuation**: *"`RecurringJobWatcher` periodically polls `RunEvent` for events that reached terminal status and inserts the next `JobRun`."* The reference has **no chaining concept at all** — `trigger_on_job_id`, `WAITING`, `ChainWatcher`, and pre-arming are project inventions layered on top. It also explicitly prescribes **"idempotent inserts"** and the principle *"react to immutable events, do not poll mutable state."*

2. **A survey of mature schedulers** (Temporal, Quartz, Google Cloud Scheduler, AWS EventBridge Scheduler, Airflow, BullMQ, sidekiq-cron) found **none** model "do B after A" with a standing pre-armed downstream. It is universally either **DAG-internal** (A and B are tasks inside one workflow definition) or **continuation** (the downstream is created when the upstream completes). Pre-arming a standing waiting entity is non-idiomatic.

**The inconsistency this exposes:** our recurrence already uses continuation (per the reference); only **chaining** diverged into pre-arm. `ChainWatcher` flipping a *mutable* `WAITING` status is itself a mild violation of the reference's "react to immutable events, don't poll mutable state" principle that continuation honours cleanly.

**Why ADR-065 did not choose continuation.** Its *Alternatives Considered* rejected *"terminate-time arming inside `ChainWatcher` (create the downstream run when the upstream terminates)"* on the grounds that it *"makes `ChainWatcher` a run-creator + data-mover — both prohibited by ADR-033."* That conflated two separable questions: **where** creation happens (`ChainWatcher` vs `RunMaterializer`) and **when** the downstream run is created (pre-armed at upstream-run-creation vs on upstream-terminal). Continuation can be done **without** putting creation in `ChainWatcher` — route it through `RunMaterializer` from the terminal-event consumer, exactly as recurring successors already are. The rejection rested on a **self-imposed layering rule, not a technical necessity**.

## Decision

### 1. Continuation run-creation
A trigger-driven (chained) downstream run is **created when its upstream run reaches a terminal status**, by the terminal-event consumer, via `RunMaterializer` — the same mechanism recurring successors already use. No downstream run is pre-created or pre-armed at upstream-run-creation time.

### 2. Remove the `WAITING` run state
The internal status machine drops `WAITING` (8 → 7 states); `wait_for_run_id` is removed. A chained downstream has **zero `JobRun`s** until its trigger fires. `trigger_on_status` becomes a **create predicate** (create the downstream run iff the upstream's terminal status matches), not a flip predicate.

### 3. Unify the consumers — delete `ChainWatcher`
`RecurringJobWatcher` generalises into one **continuation consumer**: on each terminal `RunEvent` it asks `RunMaterializer` to create **(a)** the recurring successor (if the job is recurring and not cancelled) **and (b)** one downstream run per matching trigger-driven job — both in the **same transaction**, under one cursor. `ChainWatcher` is deleted. ADR-065 rejected collapsing the two consumers (independent cursors / failure isolation); that rationale lapses because the *flip* concern no longer exists — there is exactly **one** concern, creation, and `RunMaterializer` remains its single owner.

### 4. Exactly-once by a DB unique constraint
Run creation is made **idempotent at the data layer** via a unique constraint on the run's cause: `(job_id, triggering_run_id)` for trigger-driven runs, `(job_id, scheduled_tick)` for schedule-driven recurring successors. A duplicate insert is a no-op — double-creation is impossible regardless of consumer crashes or instance count. The `processed_by` cursor is **retained only as an efficiency layer** (skip already-handled events), **not** the correctness mechanism. This realises the reference design's prescribed "idempotent inserts." Events are processed `ORDER BY event_id ASC` for causal order.

### 5. Data plane at creation (folds in ADR-064)
When the downstream run is created, `from_run_id` is set to the **upstream terminal `run_id`** directly. The executor no longer injects `from_run_id` from `wait_for_run_id` (ADR-064's injection step is removed with `WAITING`).

### 6. Slow consumer = don't create (load shedding)
If a matching downstream already has an *executing* run (`has_executing_run`: `PENDING`/`QUEUED`/`RUNNING`/`RETRYING`) when the upstream terminates, the new downstream run is simply **not created**, with an audited drop event. This replaces "arm unconditionally then drop at flip" with the same skip-create semantics recurring successors already use — nothing is created then cancelled.

### 7. Predicate-miss audit
When an upstream terminal status does **not** satisfy a downstream's `trigger_on_status`, no downstream run is created (vs today's `CANCELLED_BY_CHAIN_MISS` `WAITING` run). A lightweight audit `RunEvent` (or structured log) records the non-trigger so the decision stays observable.

### 8. Fan-out and multi-hop
**Fan-out:** on one upstream terminal, create one run per matching downstream job, independently. **Multi-hop (A → B → C):** natural event propagation — A's terminal creates B's run; B's terminal creates C's run. No recursive arming and no create-time `MAX_CHAIN_DEPTH` (chains are acyclic — `trigger_on_job_id` must reference an already-existing job).

### 9. External contract unchanged (ADR-014)
The 5 external statuses are preserved. A **not-yet-triggered** chained downstream shows as `scheduled` with an **empty `runs` list**; external status is derived at the MCP boundary from `(Job.state, latest run)`. `task.status` surfaces **`triggered_by: <job_id>`** so "what is it waiting on" stays visible (replacing the `WAITING` run's transient `wait_for_run_id` with the durable job dependency). No `task.*` schema change.

## Alternatives Considered

| Option | Reason rejected |
|---|---|
| **Keep pre-arm (status quo, ADR-065)** | Most complex, least-precedented part of the system; source of the arm-race saga; inconsistent with our own continuation-based recurring path and with the reference design. |
| **Continuation, but keep `ChainWatcher` as a second creator-consumer** | Two cursors / processes for one concern (creation) on a 1 vCPU box. The daily-digest case needs the recurring successor *and* the chained downstream created atomically per terminal event — one consumer does this cleanly in a single transaction. |
| **Exactly-once via cursor + single-instance only** | Pushes correctness onto "the consumer runs exactly once"; a crash between insert and cursor-stamp double-creates. A DB unique constraint is correctness-by-construction (the reference's "idempotent inserts"). |
| **A 6th external status ("waiting on trigger")** | Breaks the ADR-014 five-status contract and every cached client. `scheduled` + `triggered_by` conveys the same with zero client change. |

## Consequences

**Positive**
- Removes the `WAITING` state, the `ChainWatcher` process, recursive arming, `wait_for_run_id`, and the flip-time slow-consumer drop. One run-creation rule — *terminal event → materialise the next run* — governs recurrence **and** chaining.
- **Erases the pre-arm arm-race class** (#227 / #234-237) by construction: there are no pre-armed runs to race on.
- Matches the reference design and idiomatic schedulers → interview-defensible: *"why not simpler?"* → *"I did — recurrence and chaining are one continuation rule, like Celery chains / Sidekiq / BullMQ flows."*
- Exactly-once moves from operational discipline to a data-layer guarantee.

**Negative / trade-offs**
- Large blast radius on hardened code: `RunMaterializer` (drop `arm`), the consumer (absorb `ChainWatcher`), the executor (drop `from_run_id` injection), external status derivation, and a schema migration (remove `WAITING` + `wait_for_run_id`; add the unique constraint). Chain integration tests are rewritten.
- The migration must **drain or convert any live `WAITING` runs** at cutover.
- Loses the pre-fire phantom `WAITING` run row (replaced by Job-level `scheduled` + `triggered_by`). Minor observability shift.

**Follow-up**
- The **`Job.state` machine** (replacing the never-cleared `active` boolean; fixes the quota-lockout bug) is a separate, dependent decision — **ADR-068**. Decision 9's external-status derivation depends on it.
- CONTEXT.md §2 (remove `WAITING` from the internal machine) and §7 (rewrite the chaining model from pre-arm to continuation) are updated when this lands — not before, so the glossary keeps describing the as-built system until the refactor ships.
