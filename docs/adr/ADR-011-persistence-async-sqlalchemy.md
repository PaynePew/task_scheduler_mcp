# ADR-011: Persistence — async SQLAlchemy + asyncpg, per-process connection pool

- **Status**: Accepted
- **Date**: 2026-05-12
- **Source**: .doc/session/grilling-state.md Q11
- **Related**: ADR-002 (Python), ADR-003 (Postgres), ADR-010 (module layout)

## Context

The MCP SDK is async-native. The persistence layer must integrate without `asyncio.to_thread` bridging. Six processes share the DB; each must size its pool to fit under RDS `max_connections`.

## Decision

- **Async SQLAlchemy 2.0 + asyncpg** for all runtime queries.
- **Sync psycopg URL for Alembic migrations only** — async + Alembic is rough-edged; sync is rock-solid for one-shot migration jobs.
- **`expire_on_commit=False`** — committed objects remain usable for response serialisation; avoids `DetachedInstanceError` on lazy-load after commit.
- **Per-request session** (MCP handlers) and **per-iteration session** (worker loops). Never long-lived. Sessions check out a connection from the pool and return it on commit/rollback/close.
- **Connection pool sized per process role:**

  | Process | `pool_size` | `max_overflow` |
  |---|---|---|
  | mcp-server | 5 | 10 |
  | watcher | 2 | 3 |
  | worker | 5 | 10 |
  | recurring-watcher | 2 | 3 |
  | chain-watcher | 2 | 3 |

  W3 total: ~130 connections vs RDS `db.t4g.micro` `max_connections=81` ⇒ must downsize pools, upgrade to `db.t4g.small`, or add RDS Proxy.
- **`pool_pre_ping=True`** and **`pool_recycle=3600`** non-negotiable to survive idle disconnects from RDS / TCP middleboxes.

## Alternatives considered

- **Sync SQLAlchemy + `asyncio.to_thread` bridge** — works but adds context-switch overhead and complicates exception propagation. The MCP SDK pushes us toward async anyway.
- **Raw asyncpg without SQLAlchemy** — faster per query but no migrations story, no ORM, no relationships; testability suffers.
- **Tortoise ORM / SQLModel** — less mature; smaller community; weaker resume signal than SQLAlchemy.

## Consequences

- Modern Python signal in the codebase (async + type hints + pydantic v2).
- A class of async-SQLAlchemy pitfalls (`DetachedInstanceError`, `MissingGreenlet`, lazy-load on closed session) becomes a known risk; mitigations: `expire_on_commit=False`, eager loading via `selectinload`, integration tests covering serialisation paths.
- W3 connection-pool math is a real concern documented for `interview-questions.md` H6–H12.
- The choice supports both the resume narrative ("modern async stack") and the interview narrative ("here's why pool sizing matters at multi-process scale").
