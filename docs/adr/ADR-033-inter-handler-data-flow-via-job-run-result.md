# ADR-033 — Inter-handler Data Flow via `JobRun.result` + `from_run_id` Convention

**Status:** Accepted  
**Date:** 2026-05-19  
**Author:** Implementation Agent (issue #94)

---

## Context

W4 introduces several "sink" handlers (`slack_post`, `email_send`, `r2_upload`)
that need structured input produced by "source" handlers (`github_digest`,
`calendar_digest_ics`).  Two design questions arise:

1. **Where does the data travel?**  Between the upstream run finishing and the
   downstream handler executing, where is the payload stored?
2. **How does a handler declare it wants upstream data?**  Should there be a
   framework-level mechanism, or a pure convention layered above `ChainWatcher`?

The `ChainWatcher` already handles the *mechanism* (flipping `WAITING → PENDING`
when the upstream terminates).  It does not and should not touch data — its job
is status coordination only.

---

## Decision

### Data plane: `JobRun.result` (existing column)

Upstream handlers that produce structured output serialize it as a **JSON
string** into `JobRun.result` via `ActionResult.result`.  This column already
exists on every `JobRun` row; no migration is needed.

### Convention: `from_run_id: int | None` in `params_model`

Any handler whose `params_model` includes the field `from_run_id: int | None`
is a **chain-fed handler**.  When `from_run_id` is non-null at execution time,
the handler MUST:

1. Call `await read_upstream(from_run_id, session)` from
   `app.chain.upstream_reader`.
2. Dispatch on the returned `UpstreamPayload` variant before doing any other
   work:

   | Variant | Meaning | Recommended handler action |
   |---|---|---|
   | `Ok(data)` | Upstream result parsed as JSON | Use `data` as primary input |
   | `NoResult` | `result=NULL` or run not found | Use fallback / return no-op |
   | `UpstreamError(error_msg)` | Upstream failed with a diagnostic | Log / propagate error |
   | `InvalidJson(raw)` | Upstream wrote non-JSON | Log warning, skip or fail |

When `from_run_id` is null, the handler falls through to its own built-in
input (params fields, secrets, etc.) as normal.

### Deep module: `app/chain/upstream_reader`

All DB lookup + JSON parsing + variant dispatch is encapsulated in one place.
Handlers never write raw `session.execute(select(JobRun)...)` queries inline.

### Design B: `trigger_on_status=ANY` + internal branching (recommended chain pattern)

For sink-to-human chains (e.g. `github_digest → slack_post`), the recommended
pattern is:

```
upstream_job.trigger_on_status = ANY
downstream_job.params = {from_run_id: <upstream_run_id>}
```

The downstream handler internally branches on the `UpstreamPayload` variant:

- `Ok(data)` → post the digest to Slack
- `NoResult` / `UpstreamError` / `InvalidJson` → post a fallback error message to Slack

This makes the chain **self-healing**: the sink always runs (because `ANY`
flips it regardless of upstream outcome), and it always does something useful
(success message or diagnostic fallback).  Slack/email never silently fail to
notify.

---

## Anti-patterns (explicitly named)

**Do NOT** create specialised handler subclasses like:

- `slack_post_from_github_digest`
- `email_send_from_calendar_digest`
- `r2_upload_from_llm_summarize`

These names couple the sink to a specific source.  The `from_run_id` field +
`UpstreamPayload` dispatch already provides the specialisation.  Handlers are
generic; configuration (params) provides the wiring.

**Do NOT** have `ChainWatcher` copy `JobRun.result` between rows.  The watcher
is a coordination mechanism (status flips only), not a data mover.

---

## Alternatives Considered

| Option | Reason rejected |
|--------|----------------|
| Dedicated inter-handler message table | Extra schema complexity; `JobRun.result` already exists and holds exactly this data |
| Pass data through SQS message body | SQS payload has a 256 KB hard limit; Postgres `TEXT` is unbounded. Also breaks the one-DB-transaction claim-and-mark pattern |
| Framework auto-injection (chain feeds populate handler args) | Adds magic framework coupling; "convention" keeps each handler unit-testable in isolation with no framework state |
| `trigger_on_status=SUCCEEDED` + separate error chain | Two-job fan-out for error path; harder to reason about; Design B (single job, internal branching) is simpler |
| `UpstreamError` returned for failed run status (vs error_message column) | Would require a second query to check run status; `error_message` column is the canonical diagnostic field set by the executor |

---

## Consequences

**Positive:**

- Zero migration: `JobRun.result` already exists.
- Handlers remain pure functions on `(run, params)` — unit-testable without
  any chain machinery.
- `read_upstream` centralises all dispatch logic; a new variant (e.g.
  `Timeout`) can be added in one place without touching all handlers.
- `trigger_on_status=ANY` + internal branching makes sink handlers robust to
  upstream failure without needing a second downstream job.

**Negative / Trade-offs:**

- `JobRun.result` is a `TEXT` column storing raw JSON strings, not a JSONB
  column.  Postgres cannot index into the upstream result; if query-on-result
  is ever needed, a migration to JSONB would be required.
- The `from_run_id` convention is informal (not enforced by the ORM or action
  registry).  A handler that ignores it silently won't receive upstream data —
  there's no framework-level guard.
- `NoResult` covers both "run not found" and "result=NULL with no error".
  Callers that need to distinguish "run exists but empty" from "run not found"
  must add their own secondary lookup.

**Future direction:**

- If multiple upstreams per downstream are ever needed, `from_run_id` becomes
  `from_run_ids: list[int]` and `read_upstream` is called once per entry.
- Predicate-based chaining (ADR-040, deferred) would let the chain trigger only
  when specific result values are present — today's `trigger_on_status=ANY` +
  handler branching is the simpler precursor.
