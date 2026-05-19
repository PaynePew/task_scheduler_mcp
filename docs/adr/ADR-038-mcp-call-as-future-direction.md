# ADR-038: Worker as MCP Client — Future Direction (Deferred)

- **Status**: Deferred (v2)
- **Date**: 2026-05-19
- **Deciders**: PaynePew
- **Related**: ADR-013 (action catalog), ADR-014 (MCP tool surface v1)

## Context

The current action handler model (`ActionHandler.execute`) makes direct HTTP or SDK calls to external services. A natural generalisation is to treat any MCP server as an action target: the Worker becomes an MCP client, discovers tools from a remote MCP server, and dispatches `job.action_params` as a tool call.

This would enable scheduling any MCP tool call — not just the typed handlers in this registry.

## Decision

**Deferred to v2.** The `mcp_call` action type is not implemented in W4.

## v2 Sketch

```
job.action = "mcp_call"
job.action_params = {
  "server_url": "https://other-mcp-server/mcp",
  "tool_name": "files.read",
  "tool_params": { "path": "/data/report.csv" }
}
```

Worker flow:
1. `McpCallHandler.execute()` opens an MCP HTTP session to `server_url`.
2. Calls `tool_name` with `tool_params`.
3. Stores tool result in `JobRun.result` (inter-handler data plane — ADR-033).
4. Downstream chain handlers can reference the result via `from_run_id`.

## Deferral rationale

- No concrete use case has materialised that requires composing external MCP servers.
- MCP streamable-HTTP transport spec is still evolving; pinning to it now would add churn.
- The typed handler registry (ADR-013) covers all W4 use cases with less operational surface.
- Revisit when a user-facing workflow actually needs to call a third-party MCP tool.

## Alternatives considered

- **Generic `http_call` handler** — already ships; covers most outbound API call patterns without the MCP session overhead.
- **Caddy MCP composability at proxy layer** — tracked separately; complements rather than replaces this approach.

## Consequences (if implemented in v2)

- Workers need an MCP client dependency (e.g., `mcp` Python SDK or `httpx`-based minimal client).
- Auth to the remote MCP server must be handled via secrets (ADR-032 `${VAR}` substitution in `server_url` headers).
- Timeout semantics require care — an MCP tool call may itself be long-running; `ChangeMessageVisibility` heartbeat (CONTEXT.md §4) must cover the full round-trip.
