# ADR-013: Action catalog — typed action registry

- **Status**: Accepted
- **Date**: 2026-05-12
- **Source**: internal grilling session Q13 (local-only, not in git)
- **Related**: ADR-010 (module layout), ADR-014 (tool surface)

## Context

The system must support multiple kinds of executable tasks. W1 ships `echo` (smoke test) and `http_call`. W2 adds `llm_summarize`, `llm_chat`, `send_email`. Future actions must not require changes to dispatch code, the worker loop, or the MCP tool schema beyond a registry entry.

## Decision

A **typed action registry** keyed by action name. Each action implements the `ActionHandler` protocol:

```python
class ActionResult:
    ok: bool
    result: dict | None
    error: str | None
    retryable: bool = True   # False ⇒ permanent failure ⇒ DLQ

class ActionHandler(Protocol):
    name: ClassVar[str]
    params_model: ClassVar[type[BaseModel]]   # Pydantic model for action_params
    timeout_seconds: ClassVar[int]            # asyncio.wait_for budget

    async def execute(self, run: JobRun, params: BaseModel) -> ActionResult: ...
```

A module-level `ACTION_REGISTRY: dict[str, ActionHandler]` is the dispatch table. The MCP `task.create@v1` schema reads its `action` enum from `ACTION_REGISTRY.keys()`. `task.list_actions@v1` exposes the registry to clients (name, description, timeout, params JSON Schema).

W1 ships two handlers:

- `echo` — returns `{"echoed": <message>}`. Pipeline smoke test.
- `http_call` — `httpx.AsyncClient` request; result body truncated to 2 KB; `retryable = (status_code >= 500)` so server errors retry but user errors do not.

## Alternatives considered

- **Switch/case dispatch in the worker** — every new action edits worker code; violates open/closed; harder to test.
- **Dynamic plugin loading (entry points)** — over-engineering at this scale; complicates packaging.
- **Function-based registry without typing** — loses Pydantic validation; runtime errors instead of type errors.

## Consequences

- Adding a W2 action = 1 Pydantic params model + 1 handler class + 1 registry entry. Zero dispatcher changes.
- Action authors choose their own `retryable` policy per error — retry behaviour is data-driven, not hardcoded.
- Per-action `timeout_seconds` is enforced via `asyncio.wait_for`; hung actions become retryable failures, not blocked workers.
- The `task.create@v1` tool schema is generated from the registry; clients always see the current action list via `task.list_actions@v1`.
