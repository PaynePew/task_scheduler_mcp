# ADR-020 — Chain Watcher Validation

**Status:** Accepted  
**Date:** 2026-05-16  
**Author:** Implementation Agent (issue #44)

---

## Context

Issue #44 (S03) introduces Job chaining: a downstream Job can declare
`trigger_on_job_id` (another Job) and `trigger_on_status` ("SUCCEEDED" |
"FAILED" | "ANY").  The downstream Job's first `JobRun` is created in
`WAITING` status, and the `ChainWatcher` polls terminal `RunEvent`s to
flip matching WAITING runs to `PENDING` or `CANCELLED`.

Two sub-problems require design decisions:

1. **Create-time validation** — what invariants must hold before we accept
   a `trigger_on_job_id`, and what error code/field does each violation map to?
2. **Match semantics** — how does `ANY` interact with `CANCELLED`?  Does the
   "recurring A triggers one-shot B" scenario respect idempotency?

---

## Validation Rules (V1-V5)

These are enforced inside `app/domain/chain_validation.validate_chain`, which
is called from `create_job` when `trigger_on_job_id` is set.

| # | Rule | Error code | Field | Expected |
|---|------|------------|-------|---------|
| V1 | Trigger Job exists | `NOT_FOUND` | `trigger_on_job_id` | — |
| V2 | Trigger Job has same `user_id` as caller | `NOT_FOUND` | `trigger_on_job_id` | — |
| V3 | Trigger Job has at least one non-terminal run | `INVALID_STATE` | `trigger_on_job_id` | — |
| V4 | No cycle in the `trigger_on_job_id` ancestry | `USER_INPUT` | `trigger_on_job_id` | `non-circular chain` |
| V5 | Ancestor chain depth ≤ 10 (including new job) | `USER_INPUT` | `trigger_on_job_id` | `chain depth ≤ 10` |

### V1 — Trigger Job must exist

Straightforward: look up the job by `job_id`.  If absent → `NOT_FOUND`.

### V2 — Same user_id (intentional 404, not 403)

If the trigger job exists but belongs to a different user, we return the same
`NOT_FOUND` error as V1.  Returning `FORBIDDEN` / 403 would reveal that a job
with that ID exists, enabling job-ID enumeration attacks.  This is the same
"prevent enumeration" principle used in `task.cancel.v1` and `task.status.v1`.

### V3 — Trigger Job must not be fully terminated

A job is "fully terminated" when every `JobRun` row is in a terminal status
(`SUCCEEDED`, `FAILED`, `CANCELLED`).  Chaining on a fully-terminated job
would leave the new run in `WAITING` forever — there will never be a new
terminal `RunEvent` that could unblock it.

For recurring jobs this means: if the job is `active=true` but currently
between runs (last run is terminal), V3 fires.  This is intentional for W2
scope; a future slice could allow chaining on active recurring jobs by using
`wait_for_run_id = NULL` and a different watcher query.  That complexity is
explicitly deferred.

### V4 — No cycle (recursive CTE)

The ancestor walk (V4) and depth check (V5) share one recursive CTE:

```sql
WITH RECURSIVE ancestors(cur_id, next_id, depth, path, is_cycle) AS (
    SELECT job_id, trigger_on_job_id, 1, ARRAY[job_id], false
    FROM jobs WHERE job_id = :start_id

    UNION ALL

    SELECT j.job_id, j.trigger_on_job_id, a.depth + 1,
           a.path || j.job_id,
           j.job_id = ANY(a.path) AS is_cycle
    FROM ancestors a
    JOIN jobs j ON j.job_id = a.next_id
    WHERE a.next_id IS NOT NULL AND NOT a.is_cycle AND a.depth < :max_depth + 2
)
SELECT MAX(depth) AS max_depth, bool_or(is_cycle) AS has_cycle
FROM ancestors
```

**Why one CTE for both?**  Walking the ancestry is expensive (N DB round-trips
if done naively).  A single recursive CTE does it in one query and returns both
the max depth and whether a cycle was detected.  The `path || j.job_id` array
accumulates all ancestor IDs; `j.job_id = ANY(a.path)` detects re-entry.

Cycles among existing jobs should not occur if validation is always enforced,
but the CTE guard ensures the query never infinite-loops even if data integrity
was violated by a manual DB edit.

### V5 — Chain depth ≤ 10

The CTE returns `max_depth` (counting the trigger job as 1).  The new job
adds 1 more.  If `max_depth ≥ 10` (i.e. adding the new job would make the
total chain length 11 or more), V5 fires.

The limit of 10 is arbitrary but bounds the worst-case cascading fan-out when
a recurring upstream produces many terminal events.  It can be raised via the
`MAX_CHAIN_DEPTH` constant in `chain_validation.py`.

---

## `ANY` Includes `CANCELLED` — Enum Literal Interpretation

`trigger_on_status` accepts three values: `"SUCCEEDED"`, `"FAILED"`, `"ANY"`.

The match predicate in `ChainWatcher._is_match`:

```python
def _is_match(trigger_on_status: str | None, event_type: str) -> bool:
    effective = trigger_on_status or "SUCCEEDED"
    return effective == "ANY" or effective == event_type
```

**Design decision:** `ANY` matches every terminal event type, **including
`CANCELLED`**.

The alternative ("magic exclusion") would treat `CANCELLED` as special — users
who write `ANY` might not *intend* to chain on a cancelled upstream.  But we
reject this alternative because:

1. **Least surprise for the enum literal.**  `ANY` means any.  If we excluded
   `CANCELLED`, users would have to know about the hidden exception.
2. **Explicit alternatives exist.**  If a user only wants to chain on
   success-or-failure (not cancellation), they can omit `trigger_on_status`
   (defaults to `"SUCCEEDED"`) or write `"FAILED"`.  There is no `"NOT_CANCELLED"`
   enum value because `SUCCEEDED | FAILED` covers the common case.
3. **Cancel semantics are explicit (ADR-022).**  `CANCELLED` is a first-class
   terminal status from a job-level cancel.  Hiding it from `ANY` would make
   chain-on-cancel impossible to express.

This decision is flagged in tests:
```python
("CANCELLED", "ANY", "PENDING"),  # CANCELLED × ANY → PENDING — key design choice
```

---

## One-Shot Hook vs. Subscription

`ChainWatcher` implements a **one-shot hook** model, not a **subscription** model.

- **Subscription:** every terminal event from upstream re-triggers downstream
  (recurring A → recurring B: each of A's runs spawns a new run of B).
- **One-shot hook:** the downstream job's single `WAITING` run is flipped
  exactly once by the first terminal event whose `run_id` matches
  `wait_for_run_id`.

The ChainWatcher achieves one-shot semantics by design:

1. The downstream `JobRun.wait_for_run_id` points to a specific upstream
   `run_id`, not to the upstream `job_id`.
2. The `UPDATE … WHERE status = 'WAITING'` predicate is idempotent: once the
   run is flipped to `PENDING` or `CANCELLED`, subsequent ticks find no
   matching rows and do nothing.
3. The `processed_by["chain_watcher"]` cursor ensures the *same upstream event*
   is processed at most once, even if the watcher crashes mid-tick.

**Recurring A → one-shot B — once:**  When a recurring Job A produces its
first terminal run, the ChainWatcher flips B's one WAITING run.  When A
produces its *second* terminal run, the ChainWatcher processes that event
(stamps its cursor) but finds no WAITING runs with `wait_for_run_id = run_a2_id`
— because B's run was already flipped and has moved past WAITING.

This behavior is intentional for W2.  A future slice could support
"re-trigger on every cycle" by having `RecurringJobWatcher` (or a dedicated
service) inspect `trigger_on_job_id` relationships and create a fresh WAITING
run for B whenever A's new run is scheduled.  This is flagged as "subject to
revisit" per the W2 PRD scope decisions.

---

## Transactional Outbox Atomicity

The ChainWatcher's `poll_once` function runs everything inside a single
`session.begin()` transaction:

1. SELECT terminal events (not yet stamped).
2. For each event: SELECT matching WAITING runs, flip status, emit RunEvent.
3. Stamp `processed_by["chain_watcher"]` on the terminal event.

All three steps are atomic.  If the process crashes between steps 2 and 3, the
transaction rolls back and the event is re-processed on restart — idempotent
because step 2's UPDATE finds no WAITING runs (they're already flipped).

If the process crashes between finding the event and step 2, the transaction
also rolls back and the event is re-processed cleanly.

---

## Alternatives Considered

| Option | Reason rejected |
|--------|----------------|
| Trigger by `job_id` instead of `run_id` | Would need to track "which run should B wait for" separately; `run_id` is more precise and avoids ambiguity for recurring upstreams |
| V4/V5 as separate DB round-trips | Would require 2 queries per `trigger_on_job_id`; combined CTE is one query |
| Raise 403 for V2 cross-user | Reveals job existence; intentional 404 matches the rest of the API |
| `ANY` excludes `CANCELLED` | Magic exclusion violates the principle of least surprise; explicit enum values cover the common case |
| Per-cursor locking in ChainWatcher | `FOR UPDATE SKIP LOCKED` is overkill for a single-instance watcher (W1 scope); the `processed_by` cursor is sufficient |
