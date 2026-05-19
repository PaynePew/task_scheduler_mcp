# ADR-039: Plan Abstraction — Future Direction (Deferred)

- **Status**: Deferred (v2)
- **Date**: 2026-05-19
- **Deciders**: PaynePew
- **Related**: ADR-009 (schema + outbox), ADR-033 (inter-handler data plane), ADR-040 (predicate-based chain)

## Context

The current chaining model links `Job`s via `trigger_on_job_id` + `trigger_on_status` — a directed graph of `Job` nodes, each with one upstream dependency. This is sufficient for linear A→B chains and simple fan-in patterns.

A richer abstraction is a **Plan**: an atomic, named, multi-step entity that owns a DAG of `Job`s as a unit. Plans enable:

- Atomic cancel (`plan.cancel` cancels all member jobs in one operation)
- Plan-level status rollup (e.g., `PARTIAL_FAILURE` when ≥ 1 branch fails but others succeed)
- Plan-scoped result browsing (`tasks://plans/{plan_id}/runs`)
- LLM-friendly tool: `task.create_plan.v1` accepts a declarative step list

## Decision

**Deferred to v2.** No `Plan` entity is introduced in W4.

## v2 Schema Sketch

```sql
CREATE TABLE plans (
  plan_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id      TEXT NOT NULL,
  name         TEXT,
  status       TEXT NOT NULL DEFAULT 'PENDING',
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- jobs gains a nullable FK:
ALTER TABLE jobs ADD COLUMN plan_id UUID REFERENCES plans(plan_id);
```

New MCP tool:
```
task.create_plan.v1(steps: [{action, params, depends_on?: [step_index]}]) → {plan_id, job_ids}
```

## Deferral rationale

- Current `trigger_on_job_id` covers all W4 workflow use cases (digest → Slack, calendar → Slack).
- A `Plan` entity requires a migration, a new watcher, and new MCP tool schemas — non-trivial scope.
- Plan-level semantics (partial failure, rollback) are hard to define correctly without real user feedback.
- Revisit when multiple users report needing atomic multi-step cancel or plan-level status rollup.

## Alternatives considered

- **Graph stored in `jobs.metadata` JSONB** — avoids new table but makes queries complex and plan-level cancel impossible without full-table scans.
- **External orchestrator (Temporal, Prefect)** — out of scope; this project is a self-contained scheduler, not a general workflow engine.

## Consequences (if implemented in v2)

- `job.plan_id` FK is nullable in v2, so existing data is unaffected.
- `ChainWatcher` gains plan-level state-machine logic.
- `task.list.v1` gains an optional `plan_id` filter.
