# ADR-014: MCP tool surface — 5 `@v1` tools + uniform envelope + 6 error codes

- **Status**: Accepted
- **Date**: 2026-05-12
- **Source**: internal grilling session Q14 (local-only, not in git)
- **Related**: ADR-013 (action catalog), ADR-015 (user_id resolver)

## Context

MCP clients cache `tools/list` per thread. Changing a tool's input schema or output shape mid-thread breaks long conversations. The toolset must be: stable, self-describing for LLM consumption, capable of communicating actionable errors for self-correction.

## Decision

**Five `@v1`-versioned tools:**

- **`task.create@v1`** — creates a `Job` (and an initial `JobRun` for one-shot/immediate types).
- **`task.list@v1`** — returns the user's jobs newest-first; supports status filter, `created_at` range, offset pagination (`page` + `pageSize`).
- **`task.status@v1`** — returns one job; with `include_runs=true` includes recent execution history.
- **`task.cancel@v1`** — flips eligible jobs to `CANCELLED`; returns `INVALID_STATE` for terminal jobs.
- **`task.list_actions@v1`** — exposes the action registry; LLM is instructed to call this once per thread.

**Versioning suffix `@v1` is mandatory.** To evolve a tool, ship `task.foo@v2` alongside `@v1`. Never mutate the v1 schema in place.

**Uniform envelope:**

```
Success: {"ok": true, "data": {...}}
Failure: {"ok": false, "error": {"code", "message", "field", "expected"}}
```

**Six error codes:** `USER_INPUT`, `NOT_FOUND`, `INVALID_STATE`, `UNKNOWN_ACTION`, `DUPLICATE`, `INTERNAL`. Drawn from a fixed vocabulary so the LLM can branch on `code` deterministically.

**Strict input schemas.** Every tool specifies `required`, `enum`, `default`, and `additionalProperties: false`. ISO 8601 timezone-aware datetime strings throughout.

**~125-token system instruction** guides the LLM to: (a) call `task.list_actions@v1` once per thread, (b) default `timezone` to UTC, (c) default `schedule_type` to `immediate` when the user is silent.

## Alternatives considered

- **Unversioned tools** — schema evolution breaks every long-running thread; unacceptable.
- **Flexible error shape** — the LLM can't reliably extract intent; self-correction loops degrade.
- **Loose schemas (no `additionalProperties: false`)** — typos and stale params silently pass through; bugs surface late.
- **Embed action params inline in `task.create@v1`** — couples tool schema to every action's schema; every new action mutates the tool. Instead, `action_params` is a generic object validated by the registered Pydantic model.

## Consequences

- LLMs can self-correct on validation errors via `error.field` + `error.expected`. Demoably better than free-form error strings.
- The `@v1` discipline costs a few extra characters but pays back the first time we ship a schema change in W2.
- The 5-tool surface is small enough to memorise; large enough to cover all user stories without overlap.
- Internal 8 statuses are mapped to 5 external statuses at the handler boundary — DB keeps precision, LLM gets simplicity. (See `CONTEXT.md` § 2.)
