# ADR-022: Cancel semantics — best-effort for currently-running executions

**Status:** Accepted  
**Date:** 2026-05-16  
**Deciders:** PaynePew  
**Refs:** Issue #41, ADR-010 (layered module layout), ADR-014 (MCP tool surface v1)

---

## Context

W1 shipped a `task.cancel.v1` tool that cancelled all non-terminal runs atomically
including those with status `RUNNING`. This was incorrect: a RUNNING run is owned
by a worker process that has already claimed the row and begun executing the action.
Writing `CANCELLED` to its `job_runs` row from the API layer creates a split-brain
— the worker is still running, but the DB record says cancelled. The worker's
eventual `SUCCEEDED` or `FAILED` write then races with the API's `CANCELLED` write.

Additionally, W1 treated re-cancelling an already-cancelled job as `INVALID_STATE`.
This is needlessly strict: the caller's intent is "this job should be stopped", and
if it already is, the intent is satisfied. Returning an error forces the client to
first call `task.status.v1` before retrying, adding round-trips for no benefit.

---

## Decision

### 1. Best-effort for RUNNING runs

Cancel is **job-level**, not run-level. When `task.cancel.v1` is called:

- Runs with status `PENDING`, `QUEUED`, `WAITING`, or `RETRYING` are immediately
  flipped to `CANCELLED` (one `RunEvent(CANCELLED)` per run, same transaction).
- Runs with status `RUNNING` are **left untouched**. They complete naturally and
  write their own terminal `RunEvent` (`SUCCEEDED` or `FAILED`).
- `jobs.cancelled_at` is set to `now()` in the same transaction.

The caller is told upfront (in the tool description) that cancellation is
best-effort for currently-running executions. No false promise of atomicity.

### 2. `cancelled_at` as the idempotency anchor

A dedicated `jobs.cancelled_at TIMESTAMPTZ NULL` column (migration 0003) is the
single source of truth for "was a cancel requested?":

- If `cancelled_at IS NOT NULL` on entry, re-cancel is a **no-op** — the function
  returns success immediately without touching any run rows.
- Downstream components (RecurringJobWatcher, ChainWatcher) can use `cancelled_at`
  to decide whether to spawn the next recurrence without re-querying run statuses.

### 3. `INVALID_STATE` only for natural termination

`INVALID_STATE` is returned only when **all** runs are in `{SUCCEEDED, FAILED}` and
`cancelled_at` is `NULL`. This is the case where the job finished its lifecycle
naturally and there is no pending or in-flight work to cancel.

### 4. Uniform across schedule_type

The semantics above apply equally to `immediate`, `one-shot`, and `recurring` jobs.
`schedule_type` affects when new runs are created; it does not change cancellation
behaviour. (Alignment with Linux `cron`: `crontab -r` removes the schedule; it does
not kill the currently-executing cron job.)

---

## In-place change vs `.v2` tool

This change modifies `task.cancel.v1` in place. No MCP client has yet cached the
W1 schema (the only clients are test harnesses under our control), so there is no
migration burden on external callers. Bumping to `task.cancel.v2` for this fix
would split the tool surface unnecessarily. If clients existed in production with
the old contract (re-cancel = error), a `.v2` would be warranted.

---

## Consequences

**Positive:**
- Eliminates the split-brain between API cancel and worker execution.
- Idempotent cancel simplifies retry logic in MCP clients.
- `cancelled_at` gives downstream watchers a clean signal without extra queries.

**Negative / trade-offs:**
- A RUNNING job may complete `SUCCEEDED` after a cancel request. The caller sees
  `cancelled` from `task.cancel.v1`, but a later `task.status.v1` may show a
  `SUCCEEDED` run. This is expected and documented in the tool description.
- No mechanism to interrupt a long-running action mid-flight (e.g. HTTP call).
  Acceptable for W2; W3+ can explore cooperative cancellation via a shared flag.
