# ADR-068 — `Job.state` lifecycle machine replaces the never-cleared `active` boolean

**Status:** Accepted
**Date:** 2026-06-30
**Author:** PaynePew (design session, grill-with-docs)
**Amends:** ADR-055 (containment quota now counts `state='active'`), CONTEXT.md §1 + §2
**Depends on:** ADR-067 (the continuation consumer hosts the event-driven state transitions; ADR-067 §9 external-status derivation depends on this)
**Validated by:** a job-scheduler survey (deep-research, 2026-06-30) + the project's reference design `design-chatgpt-tasks.pdf` + a throwaway state-machine prototype (scenarios A–H, 8/8 — incl. quota returns to 0 for every terminal job, cancel-mid-run, terminal-race CAS)

---

## Context

`Job.active` is a boolean that defaults `true` and is **never written again anywhere in the codebase** (confirmed repo-wide). The containment quota (ADR-055) counts `active = TRUE AND cancelled_at IS NULL`, so **every job ever created consumes a per-user active-total slot forever** — a completed one-shot keeps counting, and a terminal job cannot be cancelled (`task.cancel` → `INVALID_STATE`) to free it. A user who accumulates 50 lifetime non-cancelled jobs is **permanently locked out** of creating new tasks. This was hit live during verification: the demo account sat at ~47/50 and a create was rejected with `Quota exceeded: active_total_per_user`. The bug directly contradicts the documented "100/day" creation capability.

The root cause is a **terminology drift**: ADR-055's caps exist to bound **resident / steady-state load** (*"capping creation rate does not bound the resident load of already-created recurring jobs"*), i.e. *what still consumes the box* — but the code treats `active` as a **lifetime counter** (*ever created and not cancelled*).

A scheduler survey (Temporal, Quartz, Cloud Scheduler, EventBridge, Airflow, BullMQ, sidekiq-cron) found the idiomatic model is an **explicit, operator-/event-managed schedule state** (a small state machine: `enabled`/`paused`/`completed`/`cancelled`), and that the quota counts **definition rows by state**. None derive activeness from run status at query time, and **none flip a generic boolean via a background consumer** (the original "fix" idea — flagged as a smell). One-shot completion is idiomatically a state transition to a terminal state (AWS even deletes the row). CONTEXT.md §1 was already updated to define `active` ≡ *has resident load — can still produce or run a future `JobRun`*.

## Decision

### 1. Replace `Job.active` with a `Job.state` enum
Drop the boolean. Add `Job.state ∈ { active, completed, cancelled }` (`paused` deferred — YAGNI; the enum is extensible). Semantics:

- **`active`** — has resident load: can still produce or run a future `JobRun`.
- **`completed`** — the schedule is **exhausted**: it will produce no more runs. **This is not "succeeded"** — run success/failure stays on `JobRun.status`. A one-shot whose only run *failed* is still `completed` at the Job level.
- **`cancelled`** — a user explicitly stopped it; no more future runs.

### 2. Transition rules
| Job kind | Transition | Driven by |
|---|---|---|
| `immediate` / `one-shot` | `active → completed` when its single `JobRun` reaches a terminal status | the continuation consumer (ADR-067), reacting to the terminal `RunEvent` |
| `recurring` (schedule-driven) | stays `active` until cancelled — **never auto-flipped by a run event** | — |
| `trigger-driven` (chained) | `active → completed` when its trigger parent is terminal (`completed`/`cancelled`) **and** it has no non-terminal run | continuation consumer, via **one-hop parent propagation** (below) |
| any | `active → cancelled` | `task.cancel` |

**Parent propagation (chained jobs).** A chained job `B` (`trigger_on_job_id = A`) settles to `completed` when `A.state ∈ {completed, cancelled}` **and** `B` has no non-terminal run. Only the **immediate parent** is read; `active` propagates **downward** along the chain (when `A` settles, `B` settles on its next terminal/parent check; then `C`, …). Under ADR-067 there is no `WAITING` run, so "no non-terminal run" means no `PENDING/QUEUED/RUNNING/RETRYING` run.

### 3. Quota counts `state`
Containment becomes `COUNT(*) WHERE state = 'active'` (and `job_type = 'recurring'` for the ≤5 recurring cap). The partial index `idx_jobs_active_recurring` becomes `WHERE state = 'active' AND job_type = 'recurring'`. `cancelled_at` is no longer part of the quota predicate (it remains an audit timestamp).

### 4. Cancel is decoupled from in-flight runs
`task.cancel` sets `state = cancelled` (+ `cancelled_at`), which stops **future** runs, and **leaves any in-flight `JobRun` to finish** by default (best-effort — unchanged behaviour, and the idiomatic schedulers' default). A forceful "terminate the in-flight run" path is **deferred** (a separate explicit operation, per the Temporal cancel-vs-terminate distinction).

### 5. Terminal-race resolution — compare-and-set
The one-shot `→ completed` transition and a concurrent user `→ cancelled` race. Both are expressed as **CAS from `active`**: `UPDATE jobs SET state = :target WHERE job_id = :id AND state = 'active'`. The **first commit wins**; the loser matches zero rows and no-ops. Both targets exit the active count, so the quota is correct either way. Semantic outcome: a job cancelled while its run finishes anyway stays `state = cancelled` (user intent) with `JobRun.status = succeeded` (what happened) — a coherent split of *schedule lifecycle* from *execution outcome*. `task.cancel` on an already-terminal job continues to return `INVALID_STATE`.

### 6. Backfill migration
Add the column, then backfill from existing data in one migration:
- `cancelled_at IS NOT NULL` → `cancelled`
- recurring (`cron_expr IS NOT NULL`), not cancelled → `active`
- one-shot/immediate whose single run is terminal → `completed`; still pending/running → `active`
- chained jobs → derive: `active` if the parent is still active or the job has a non-terminal run, else `completed`/`cancelled`

Then drop the `active` column and rebuild the partial index. This unblocks the live demo account (~47/50) by reclassifying its terminal jobs as `completed`.

## Alternatives Considered

| Option | Reason rejected |
|---|---|
| **Keep the `active` boolean, just flip it on terminal events** | The survey flags "background consumer flips a generic boolean on terminal run events" as a smell (the nearest precedent, Temporal `--pause-on-failure`, failed adversarial verification). A boolean also can't distinguish `completed` (exhausted) from `cancelled` (user-stopped) — losing operator/audit clarity. |
| **Derive activeness at query time, no stored state** | Correct, but pushes a join over `JobRun` status onto the hot `task.create` path on a 1 vCPU box; every surveyed system **materialises** state for cheap quota queries and clear operator semantics. |
| **Delete terminal rows (AWS `ActionAfterCompletion=DELETE`)** | We retain job history for `task.list`/`task.status` and the `RunEvent` audit log; a soft terminal `state` keeps the row out of the quota **and** preserves history. |

## Consequences

**Positive**
- Fixes the quota-lockout at its root: quota = count of `state='active'`; terminal jobs leave the active set, so the "100/day" capability is real again.
- Clear operator/audit semantics: `completed` vs `cancelled` vs `active`, with `JobRun.status` carrying the orthogonal success/failure truth.
- The only event-driven transition (one-shot/chained `→ completed`) is **co-located in ADR-067's continuation consumer** — the same place that already reacts to every terminal `RunEvent` — so no extra process or "settle watcher" is introduced.

**Negative / trade-offs**
- A backfill migration over existing jobs is required (and must run before the new quota query goes live, or counts will be wrong mid-deploy).
- The terminal-race CAS and the chained parent-propagation are **our own extension** (the survey found no precedent for chained-schedule activeness) — the highest-novelty part, to be tested hardest.

**Follow-up**
- CONTEXT.md §1 (formalise the `state` enum) and §2 (note the Job-level lifecycle alongside the run-level status machine) updated when this lands.
- `paused` (and a future `task.pause`/`task.resume`) can be added to the enum without schema churn if user-pause is ever wanted.
