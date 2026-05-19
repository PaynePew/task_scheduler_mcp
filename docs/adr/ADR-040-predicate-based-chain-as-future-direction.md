# ADR-040: Predicate-Based Chain — Future Direction (Deferred)

- **Status**: Deferred (v2)
- **Date**: 2026-05-19
- **Deciders**: PaynePew
- **Related**: ADR-020 (chain-watcher validation), ADR-033 (inter-handler data plane), ADR-039 (Plan abstraction)

## Context

The current chain trigger model is binary: `trigger_on_status ∈ {SUCCEEDED, FAILED, ANY}`. `ChainWatcher` flips a downstream `WAITING` run to `PENDING` or `CANCELLED` based solely on whether the upstream run reached the expected terminal status.

A more expressive model allows the trigger condition to **inspect the upstream result payload** (`JobRun.result`) and apply a predicate — enabling conditional branching:

```
"only trigger step B if step A's result.count > 0"
"skip step C if step B's result.status_code == 429"
```

## Decision

**Deferred to v2.** Predicate-based chaining is not implemented in W4.

## Candidate predicate languages considered

| Language | Pros | Cons |
|---|---|---|
| **jq** | Widely known; rich JSON path + filter expressions | Security: arbitrary execution; sandboxing required |
| **CEL (Common Expression Language)** | Sandboxed by design; Google-backed; typed | Python library (`cel-python`) is less mature |
| **Lua (via `lupa`)** | Full scripting; embeddable | Overkill for condition evaluation; larger attack surface |
| **JSONPath + simple comparison** | Zero new deps; trivially sandboxable | Limited expressiveness (no arithmetic, no string ops) |

## Deferral rationale

- All W4 digest workflows (digest → Slack) are unconditional: run B when A succeeds. The binary `trigger_on_status` model is sufficient.
- Predicate evaluation introduces a sandboxing requirement — any arbitrary expression engine creates a code-execution surface accessible via MCP tool calls.
- The right predicate language depends on user feedback about actual branching patterns. Picking one prematurely locks in the wrong abstraction.
- Revisit when a concrete use case requires result-conditional branching and a security model for predicate evaluation has been designed.

## v2 Sketch

```
job.trigger_predicate = "input.result.new_issues_count > 0"  # CEL expression
```

`ChainWatcher` evaluates `trigger_predicate` against `upstream_run.result` (ADR-033 data plane). If false, the downstream run is cancelled rather than activated.

## Consequences (if implemented in v2)

- `jobs` table gains a nullable `trigger_predicate TEXT` column (migration required).
- `ChainWatcher` gains a predicate evaluation step; sandbox isolation must be documented.
- `task.create.v1` schema gains an optional `trigger_predicate` field.
- The binary `trigger_on_status` remains the default when `trigger_predicate` is null — backwards compatible.
