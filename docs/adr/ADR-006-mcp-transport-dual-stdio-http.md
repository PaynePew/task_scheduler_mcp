# ADR-006: MCP transport — dual stdio + Streamable HTTP from one codebase

- **Status**: Accepted
- **Date**: 2026-05-12
- **Source**: doc/session/grilling-state.md Q6
- **Related**: ADR-010 (module layout), ADR-015 (user_id resolver)

## Context

MCP supports three transports: stdio, Streamable HTTP (SSE), and legacy WebSocket. The demos we need to support:

- Claude Desktop integration — must be stdio (Claude Desktop launches the MCP server as a subprocess).
- MCP Inspector for the course's verification step — stdio.
- Cloud demo on AWS ECS Fargate — must be HTTP-shaped to route through ALB.

## Decision

**Dual transport from a single codebase.** The MCP server module is transport-agnostic. Two entrypoints select the transport:

- **stdio entrypoint** — `python -m app.entrypoints.mcp_stdio`. Reads `MCP_USER_ID` env var.
- **HTTP entrypoint** — `python -m app.entrypoints.mcp_http`. Wraps the same server in `streamable_http_server` and listens on `$PORT`. Reads optional `X-User-Id` header.

A `--transport stdio|http` flag may also select transport at the unified entrypoint.

## Alternatives considered

- **stdio-only** — no cloud demo; weakens the AWS narrative.
- **HTTP-only** — no Claude Desktop integration; weakens the "real client" demo.
- **Two separate codebases** — duplicates the handler/registry/error-mapping layer; double the testing surface.

## Consequences

- The MCP server module must depend only on the abstract server interface, not on transport-specific types. Handler functions are `(db_session, args) -> result` — transport-free.
- User identity is resolved differently per transport (header vs env var); a single resolver function handles both (see ADR-015).
- "Same Python code serves Claude Desktop locally and ECS Fargate behind ALB in production" — a deliberate resume-narrative point.
