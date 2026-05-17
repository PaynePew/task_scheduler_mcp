# ADR-016: Cron Semantics — 5-field POSIX, DST Handling, Forbid Concurrency

**Status:** Accepted  
**Date:** 2026-05-16  
**Issue:** #43 (S02: Recurring cron expansion + timezone resolver)

---

## Context

The W2 recurring-job watcher needs a cron expression format and a clear contract for DST transitions, so that:
- LLM clients can construct expressions reliably
- Operators know exactly when jobs fire across timezone boundaries
- The scheduler never runs two instances of the same recurring job simultaneously

Three questions must be decided:
1. How many cron fields are accepted?
2. What happens when a scheduled wall-clock time is ambiguous (fall-back) or non-existent (spring-forward)?
3. How is "Forbid concurrency" enforced?

---

## Decision

### 1. Expression format: 5-field POSIX + 5 shortcuts

Accepted expressions:
- **5-field POSIX** — `minute hour dom month dow` (croniter's standard set)  
  Examples: `0 8 * * *`, `*/15 * * * *`, `30 6 * * 1-5`
- **5 standard shortcuts** — `@daily`, `@hourly`, `@weekly`, `@monthly`, `@yearly`

Rejected expressions:
- **6-field (with seconds)** — too granular; scheduler granularity is minutes
- **`@reboot`** — not meaningful for a queued job system
- **`@every <duration>`** — non-standard; use a fixed-field expression instead

**Rationale:** 5-field is universal (Unix cron, AWS EventBridge, Kubernetes CronJob). Adding seconds precision would require sub-minute polling, conflicting with the watcher's 5-second sleep cadence. `@reboot` and `@every` are extensions beyond the POSIX standard and add complexity without clear benefit at this scale.

### 2. DST handling: croniter defaults

DST transitions are handled by `croniter` using Python's `zoneinfo` module. The contract is:

| Transition | Scheduled time | Behaviour |
|---|---|---|
| **Spring-forward** | `0 2 * * *` `America/Los_Angeles` on 2026-03-08 | 2:00 AM does not exist; croniter advances to the next valid minute — **3:00 AM PDT** |
| **Fall-back (first)** | `30 1 * * *` `America/Los_Angeles` on 2026-11-01, ref = 1:00 AM PDT | Fires at **1:30 AM PDT** (-07:00) as normal |
| **Fall-back (second)** | Same cron, ref = 1:35 AM PDT (just after first occurrence) | Fires again at **1:30 AM PST** (-08:00) — both occurrences are yielded |

The consequence: a cron like `30 1 * * *` fires **twice** on fall-back day (once at each wall-clock occurrence). This is the mathematically correct cron interpretation and matches the behaviour of Linux cron, Kubernetes CronJobs, and AWS EventBridge (which also use UTC-normalised scheduling). Operators who want exactly one firing on fall-back day should schedule at a time that does not occur during the fold (e.g., `0 2 * * *` rather than `30 1 * * *`).

**Inclusive-of-now first run:** `next_after(cron_expr, tz, ref)` uses `start_time = ref − 1µs` so that if `ref` falls exactly on a cron boundary, that boundary is treated as "the current occurrence" and returned immediately. This ensures that a job created at exactly 08:00:00 with cron `0 8 * * *` fires today, not tomorrow.

### 3. Forbid concurrency: event-driven spawn

The `RecurringJobWatcher` is the sole spawner. It only inserts a new `JobRun` when it observes a **terminal `RunEvent`** (`SUCCEEDED | FAILED | CANCELLED`) for the previous run. Because:

- One terminal event → one `JobRun` insert, atomic per event
- The `processed_by["recurring_watcher"]` cursor is stamped in the same transaction
- The cursor ensures each event is consumed exactly once, even if the watcher restarts

…there is never more than one PENDING/RUNNING `JobRun` for a recurring `Job` at any point. This is Forbid concurrency without any explicit lock or semaphore.

---

## Consequences

### Positive
- Simple, auditable implementation: any terminal event triggers exactly one follow-up row
- No separate concurrency-control table or advisory lock needed
- Consistent with existing `processed_by` outbox pattern (CONTEXT.md §5)
- DST behaviour is predictable and documented

### Negative / Trade-offs
- Fall-back day triggers two firings for `*:30 1:* * *`-style expressions; operators must be aware
- Sub-minute scheduling is not supported (and not planned until W4+)
- `@reboot` semantics cannot be expressed; one-time "run on start" use-cases need a one-shot job instead

---

## Addendum: Spawn-time uniqueness (2026-05-17, issue #53)

**Bug discovered:** Under tight cron cadences (e.g. `* * * * *`), the Watcher's
5-minute lookahead window causes runs to be claimed and executed *before* their
`scheduled_at`. When such a run terminates early, its terminal event computes
the same `next_after()` as the event that spawned it (both `occurred_at` values
fall in the same 60-second cron tick window). If both events appear in the same
`poll_once` batch, two identical `scheduled_at` values are inserted — violating
Forbid concurrency.

**Fix (two layers):**

1. **DB invariant — partial unique index:**
   ```sql
   CREATE UNIQUE INDEX uq_job_runs_job_scheduled_nonterminal
     ON job_runs (job_id, scheduled_at)
     WHERE status IN ('PENDING','QUEUED','WAITING','RUNNING','RETRYING');
   ```
   Prevents two non-terminal `JobRun`s from sharing `(job_id, scheduled_at)`.
   Migration `0004` adds this index.

2. **Application pre-check in `poll_once`:**
   Before inserting a new `JobRun`, `poll_once` checks whether any non-terminal
   run already exists for the same `job_id`.  If one does, the spawn is skipped
   and the event is still stamped as processed.  Within a single batch, the
   `session.flush()` after the first insert makes the new PENDING row visible to
   the check for subsequent events in the same batch, collapsing duplicate spawns
   to exactly one.

**Scope of protection:**
- The pre-check guards the same-batch case (two events with the same cron-tick
  window both unprocessed when `poll_once` runs).
- The unique index provides defence-in-depth against any future code path that
  bypasses the application-level check (e.g. a second watcher instance racing
  under a future scale-out).
