# ADR-064 — Recurring-Chain `from_run_id` Runtime Auto-Wire

**Status:** Accepted  
**Date:** 2026-05-23  
**Author:** Implementation Agent (issue #202)  
**Amends:** ADR-033 (inter-handler data flow via `JobRun.result` + `from_run_id` convention)

---

## Context

ADR-033 established that chain-fed handlers declare `from_run_id: int | None` in their
`params_model`, and that the caller sets this field **at job create time** with the
upstream run's `run_id`.  This works for **one-shot chains** (single downstream job
created pointing at a single upstream run), but is broken for **recurring chains**.

### The recurring-chain gap

In a recurring chain:

- `wait_for_run_id` on `JobRun` is auto-derived per tick in `app/domain/jobs.py` by
  `ChainWatcher`: it points to the upstream tick's run that just finished.
- `from_run_id` in `Job.action_params` is fixed at job create time as a hard-coded
  `int` (or `None` if the caller leaves it unset).

The two fields were **decoupled** — no runtime bridge existed.  Every downstream tick
of a recurring chain would read the same first-tick upstream `run_id` forever (or `None`,
causing the handler to skip the data plane entirely).

### Evidence of the gap

- `grep -r "from_run_id" app/workers/` returned zero matches before this fix.
- All existing `from_run_id` integration tests used a hard-coded upstream `run_id`.
- No test exercised recurring + chain + `from_run_id` together.

---

## Decision

### Control plane vs data plane

| Field | Plane | Who sets it | When |
|---|---|---|---|
| `JobRun.wait_for_run_id` | **Control** — when to start | `ChainWatcher` | Per tick, at flip time |
| `params.from_run_id` | **Data** — what to read | Caller (job create) or executor (runtime) | Job create for one-shot; runtime for recurring |

### Runtime injection in the executor

In `app/workers/executor.py`, immediately after `params_model.model_validate()` and
before `handler.execute()`, the executor now checks:

```python
if hasattr(params, "from_run_id") and params.from_run_id is None:
    if run.wait_for_run_id is not None:
        params = params.model_copy(update={"from_run_id": run.wait_for_run_id})
```

**Invariants preserved:**

| Case | `params.from_run_id` at create | `run.wait_for_run_id` | Injection? | Result |
|------|-------------------------------|----------------------|------------|--------|
| One-shot chain (caller sets it) | non-null | any | No (`is None` fails) | Uses caller-supplied value |
| Recurring chain (this fix) | None | non-null | Yes | Injected from `wait_for_run_id` |
| Non-chained job | N/A (no field) | null | No (`hasattr` fails) | No change |
| Non-chained recurring | None | null | No (second condition fails) | No change |

### Pydantic immutability

Handler `params_model` instances are Pydantic models.  `model_copy(update=...)` creates
a new instance rather than mutating the original — this is intentional and consistent
with Pydantic v2 best practice.

---

## Alternatives Considered

| Option | Reason rejected |
|--------|----------------|
| Set `from_run_id` in `Job.action_params` at ChainWatcher flip time | ChainWatcher is a coordination mechanism (status flips only), not a data mover — ADR-033 explicitly prohibits this pattern |
| Add a new `runtime_from_run_id` column to `JobRun` | Extra schema; `wait_for_run_id` already carries the value — no new column needed |
| Require callers to omit `from_run_id` in recurring chains and handle None in each handler | Each handler would need to re-implement the "check wait_for_run_id" logic; centralising in the executor eliminates duplication |
| A separate "chain resolver" service between ChainWatcher and executor | Extra service boundary for a two-line fix; premature complexity |

---

## Consequences

**Positive:**

- Recurring chains now correctly wire `from_run_id` per tick — the canonical "daily
  digest" use case (`github_digest → llm_summarize → slack_post`) works end-to-end
  without an external cron workaround.
- Zero behaviour change for one-shot chains or non-chained jobs.
- No schema migration: `wait_for_run_id` already exists on `JobRun`.
- Injection is transparent to handlers: they receive a populated `params.from_run_id`
  regardless of whether it came from job create or runtime injection.

**Negative / Trade-offs:**

- Implicit behaviour: a caller who sets `from_run_id=None` intending "no upstream read"
  on a run that happens to have `wait_for_run_id` non-null will be surprised to find
  the field injected.  In practice this case does not arise (a job either uses the
  `from_run_id` field or it doesn't), but the `is None` + `wait_for_run_id non-null`
  condition is the intended signal for "recurring chain data wiring".

**Follow-up (out of scope here):**

- ADR-033's anti-pattern section should be updated to note that one-shot wiring
  (`from_run_id` at create time) remains valid; recurring wiring now goes through
  runtime injection (this ADR).
- DAG chains (one downstream reads multiple upstreams) require a different design —
  `from_run_ids: list[int]` and multi-call `read_upstream` — deferred to a future issue.
