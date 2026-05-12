# ADR-002: Implementation language — Python 3.12+

- **Status**: Accepted
- **Date**: 2026-05-12
- **Source**: .doc/session/grilling-state.md Q3

## Context

The MCP server can be built in any language with an MCP SDK (Python, TypeScript, Go, Rust, Java). Language choice cascades into framework, deployment shape, async idioms, and resume narrative.

## Decision

**Python 3.12+**, dependencies managed by **`uv`** (Astral's Rust-based package manager).

## Alternatives considered

- **TypeScript** — first-class MCP SDK and event-loop semantics, but weaker resume signal for the Backend/Infra role we target; library ecosystem for distributed-systems work (Alembic-equivalent, robust async DB) is less mature.
- **Java / Spring Boot** — strong infra signal but verbose; slower iteration; MCP SDK less mature; cold-start cost on AWS Lambda (D-1 path) is significant.
- **Go** — excellent for the worker/watcher processes, but no first-class MCP SDK at decision time, and LLM-ecosystem libraries are weaker.

## Consequences

- Async-native stack throughout: `asyncio`, async SQLAlchemy + asyncpg, async MCP SDK. No `asyncio.to_thread` bridges.
- `uv` over pip — 10–100× faster installs; modern signal in `pyproject.toml`.
- Pydantic-settings for typed env config; pydantic v2 for action params modelling.
- AWS Lambda cold-start cost noted as a W4 (D-1) risk for any Lambda-based worker variant.
