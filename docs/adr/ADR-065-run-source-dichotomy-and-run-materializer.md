# ADR-065 — Run-Source Dichotomy & the `RunMaterializer`

**Status:** Accepted
**Date:** 2026-06-11
**Author:** PaynePew (design session, grill-with-docs)
**Amends:** ADR-064 (corrects its premise + scope), ADR-020 (graduates the deferred "subscription" run source), ADR-033 (extends the chaining model), CONTEXT.md §7
**Implements:** the recurring fan-out chaining capability that #108 was blocked on

---

## Context

The canonical product use case — a **recurring daily digest**
(`github_digest → llm_summarize → slack_post + email_send`, firing automatically
each morning) — could not be stood up. A chained downstream job fires **once**,
not per upstream tick.

### Why it was broken, structurally

Run *creation* is scattered across two sites that each know only their own trigger,
and the system conflated "recurrence" with "having a `cron_expr`":

- `app/domain/jobs.py:create_job` creates a chained downstream's **single** initial
  `WAITING` run (via `validate_chain`) — exactly once, at job-create time.
- `app/workers/recurring_watcher.py` spawns next runs **only** for jobs with
  `cron_expr IS NOT NULL` (`:59`), as bare `PENDING` runs with no `wait_for_run_id`
  and no trigger awareness (`:130-136`). It is really a *cron expander* named
  `RecurringJobWatcher`; a chained downstream has no cron, so it is structurally
  invisible to it.
- `app/workers/chain_watcher.py` only **flips** existing `WAITING` runs (`:113-114`);
  it never creates runs (correct per ADR-033 — it is a status coordinator, not a
  run creator or data mover).

Net: the recurring *source* runs daily, but nothing re-creates a `WAITING`
downstream run for ticks ≥ 2. **No component owns the concept "an upstream run
causes a downstream run."**

### Why ADR-064 did not fix it

ADR-064 added executor injection of `from_run_id = wait_for_run_id` — the **data
plane** last mile. But its Context claims "`wait_for_run_id` … is auto-derived per
tick … by `ChainWatcher`" — **this is factually wrong**: `wait_for_run_id` is set
once at create time, and `ChainWatcher` never sets it. ADR-064 fixed data-plane
wiring on top of a **control plane that never re-arms**, then declared the daily
digest "works end-to-end." The accompanying test (`tests/integration/test_chain_recurring.py`)
**fakes** the downstream-run creation (`_insert_downstream_run` pre-sets
`wait_for_run_id`), so the suite is green while the real spawn path is never
exercised. ADR-020's "One-Shot Hook vs. Subscription" section correctly named this
gap and deferred it ("subject to revisit"). This ADR implements it.

---

## Decision

### 1. Run-source dichotomy (the missing abstraction)

Every `JobRun` is materialized by exactly one **run source**, and the two kinds are
**mutually exclusive per `Job`**:

| Run source | Cause | Field |
|---|---|---|
| **schedule-driven** | a clock tick | `cron_expr` / `scheduled_at` (chain root) |
| **trigger-driven** | an upstream run was created | `trigger_on_job_id` (chained downstream) |

A chained job's recurrence is **inherited** from its trigger — it carries **no
`cron_expr`** of its own.

### 2. `RunMaterializer` — single owner of run creation (the L2 fix)

Introduce one **stateless domain module** (like `chain_validation.py` — *not* a new
process/service; deployment topology unchanged) that owns *what runs should exist
and in what initial state*. Both existing creation sites delegate to it:

- `create_job` → `materialize_initial(job)` — first run: a scheduled run for a
  schedule-driven job, or a `WAITING` run armed against the trigger's current
  non-terminal run for a trigger-driven job (today's `validate_chain` path).
- `RecurringJobWatcher` → `materialize_successor(terminated_run)` — the next cron
  occurrence.

**Arming the downstream is an *internal, atomic step* of materialization**: a helper
`arm(upstream_run)` materializes one fresh `WAITING` downstream run per downstream
job (`wait_for_run_id = upstream_run`) and recurses (bounded by `MAX_CHAIN_DEPTH`).
Because `arm` runs inside the same transaction that created the upstream run, **you
cannot create a run without arming its downstream** — chain coordination becomes
*structural*, not convention-based. One shared low-level primitive performs the
insert + `CREATED` `RunEvent` + forbid-concurrency check (consolidating logic
previously hand-rolled in both `create_job` and `recurring_watcher`).

`ChainWatcher` (status flip) and the executor (`from_run_id` injection, ADR-064)
are **unchanged**. Three concerns stay separate: materialize *creates*, ChainWatcher
*coordinates status*, executor *moves data*.

### 3. V6 — `trigger_on_job_id` ⊥ `cron_expr` (hard reject)

`task.create` rejects a job that sets **both** `trigger_on_job_id` and `cron_expr`
with `USER_INPUT` (added to `validate_chain`, alongside V1–V5). Encodes the
run-source dichotomy as an enforced invariant and closes the double-spawn footgun
that #202's test embodied. Create-time only — no migration, no effect on historical
rows.

### 4. Slow-consumer policy — drop (load-shedding)

If an upstream outpaces a downstream, the overlapping downstream tick is **dropped**,
preserving the invariant **at most one *executing* `JobRun` per `Job`** —
`PENDING` / `QUEUED` / `RUNNING` / `RETRYING`. A `WAITING` run armed for the *next*
tick is **not** executing, so it may coexist with the current tick's executing run.

Implemented via the shared `has_executing_run(job)` predicate (executing statuses
only — `WAITING` excluded):

- **Spawn time** (cron roots, `RecurringWatcher` → `materialize_successor`): forbids a
  second *executing* root run for the same scheduled tick.
- **Flip time** (trigger-driven downstreams, `ChainWatcher`): when a `WAITING` run
  would flip to `PENDING` but the downstream already has an executing run, the tick is
  dropped — `WAITING → CANCELLED` with an audited `CANCELLED_SLOW_CONSUMER` event.

Arming is therefore **unconditional per tick**: `materialize_successor` always creates
the full `WAITING` cascade, even while the previous tick's downstream work is in flight.
The flip-time drop is the single, *audited* load-shedding path. Because the spawn-time
check no longer counts `WAITING`, the predicate needs no `exclude_run_id`.

> **Correction (operator decision, 2026-06-12, supersedes #227):** the original wording
> ("at most one *live* run", with the spawn-time check applied to all spawns) caused a
> real race in production. Under concurrent watchers, `RecurringWatcher` arms tick N+1
> within ≤ 5 s of the upstream's terminal event — while the downstream's tick-N run is
> still live (`WAITING`, not yet flipped, or `PENDING`/`RUNNING`). The old `has_live_run`
> counted that run and raised `ConcurrencyError`, so `_arm` silently skipped the entire
> downstream subtree: the chain went dark every other tick, with no audit record. The
> race competes against the **watcher poll phase**, not the 24 h tick interval — so the
> earlier "never triggers in practice" claim was wrong. Redefining the invariant to count
> only *executing* runs makes arming unconditional and routes all load-shedding through
> the single audited flip-time drop. This **supersedes #227's literal AC** ("at no point
> do two non-terminal runs exist for the same job"): a transient overlap of two `WAITING`
> runs (the not-yet-flipped current tick + the newly armed next tick) is now expected and
> is resolved by the same terminal event's flip.

---

## Alternatives Considered

| Option | Reason rejected |
|---|---|
| **L1 — `arm_downstream` helper only** (shared INSERT, three deciders) | Consolidates the plumbing but not the *decision*; run creation stays scattered across create_job / recurring_watcher / arm. A detour, not a root fix. |
| **L3 — collapse `RecurringWatcher` + `ChainWatcher` into one event-reactor** | Sacrifices the two outbox consumers' independent cursors / failure isolation; a high-risk rewrite of battle-tested HA code for purity the single-operator deployment doesn't need. The two *loops* are not the flaw; scattered *creation* is. |
| **Terminate-time arming inside `ChainWatcher`** (create the downstream run when the upstream terminates) | Makes `ChainWatcher` a run-creator + data-mover — both explicitly prohibited by ADR-033. Creation ≠ coordination. |
| **Queue overlapping ticks** (slow consumer) | Unbounded backlog; actively wrong for digests (would deliver stale content). Drop matches existing recurring semantics. |
| **Soft-tolerate `cron + trigger`** (ignore cron, warn) | Silently mutates caller intent (the same "implicit behaviour" trap ADR-064 got burned by) and lets the wrong mental model survive. Fail loud instead. |

---

## Consequences

**Positive:**

- Recurring fan-out chaining works per tick; #108's daily digest can go live with
  **no `task.create` API change** (the schema already carries every needed field).
- "What causes a run to exist" is answerable in **one place**; the run-source
  invariant is enforced, not assumed. Runtime architecture is *simpler* (one creation
  owner), not more complex — no new process.
- One-shot chains and non-chained recurring jobs are unchanged (an upstream with no
  successor runs never re-arms; `arm` on a job with no downstream is a no-op).

**Negative / Trade-offs:**

- Larger blast radius than a patch: `create_job` and `RecurringWatcher` are refactored
  to delegate to the `RunMaterializer`. Done under TDD with the existing suites
  (`test_create_job`, `test_chain_watcher`, recurring-watcher tests) as the regression
  net.
- **`tests/integration/test_chain_recurring.py` must be rewritten** to exercise the
  real `materialize → arm → ChainWatcher flip → executor inject` path instead of
  faking downstream-run creation. The current green test is misleading and is a
  net liability until replaced.

**Follow-up:**

- ADR-064 amended to scope it to the data plane and retract the "works end-to-end"
  claim. ADR-020's deferred "subscription" model marked as realised here. Fan-in
  (`from_run_ids: list[int]`) remains deferred (ADR-033 future / ADR-040).
