# ADR-010: Module layout — layered `app/{config,db,domain,mcp,workers,queue,actions,entrypoints}/`

- **Status**: Accepted
- **Date**: 2026-05-12
- **Source**: internal grilling session Q10 (local-only, not in git)
- **Related**: ADR-002 (Python), ADR-004 (ECS Fargate), ADR-006 (transport)

## Context

Six logical processes share a single codebase. The layout must support: (1) unit-testing handlers without MCP transport, (2) one Dockerfile serving all six entrypoints, (3) 1:1 mapping to W3 ECS service definitions, (4) low-friction addition of new tools and actions.

## Decision

**Layered structure** under a single `app/` package:

```
app/
├── config/        # pydantic-settings; single source of truth for env vars
├── db/            # engine, async session factory, ORM models, repository functions
├── domain/        # business logic (create_job, claim_run, complete_run, fail_run)
│                  # knows nothing about MCP
├── mcp/           # tool definitions, handlers, registry, error formatter, server wiring
├── workers/       # watcher / executor / recurring_watcher / chain_watcher loops
├── queue/         # SQS client wrapper (send/receive/delete/visibility/heartbeat)
├── actions/       # action handlers + registry
└── entrypoints/   # six python -m targets, ~10 lines each
```

Handler / domain / repository three-layer separation:

- **Handler** (`app/mcp/handlers/`) — wraps domain calls; does MCP envelope serialisation; maps domain exceptions to error codes.
- **Domain** (`app/domain/`) — pure business logic; takes a session + args, returns plain objects.
- **Repository** (`app/db/repositories/`) — only SQLAlchemy queries; no business decisions.

## Alternatives considered

- **Flat package** (`app/*.py`) — fast at first, but the line between MCP-layer concerns and worker concerns blurs; hard to test handlers in isolation.
- **Vertical slices by feature** (`app/create_task/`, `app/cancel_task/`) — duplicates DB/queue code across slices; awkward when an entrypoint cares about all slices.
- **Microservices (separate codebases per process)** — destroys the "one image, six entrypoints" benefit; multiplies CI/CD overhead.

## Consequences

- Each entrypoint is ~10 lines: import the right loop or server, wire config, run.
- Handlers are pure functions on `(db_session, args)` — unit-testable without spawning MCP transport.
- W3: each `app/entrypoints/*.py` becomes one ECS Fargate service definition (same image, different `command:`).
- W4 (D-1 Lambda variant): swap the worker entrypoint for a Lambda handler with no business-logic changes.
- The domain layer becomes the canonical place to write integration tests against real Postgres.

## Migrations boundary

`migrations/` sits **below** the `app/` package in the dependency order and is
explicitly excluded from the "Settings is the single source of truth for env
vars" rule.

**Rationale:** Alembic migrations are versioned, append-only snapshots. A
migration at revision 0005 must encode the env contract that existed _when it
was written_, not whatever `app/config/settings.py` says years later. Importing
`from app.config import settings` couples the migration to a moving target: a
field rename or type change in Settings could break replay against a fresh
database long after the original deploy.

`migrations/env.py` already follows this pattern (it reads `ALEMBIC_DATABASE_URL`
directly from `os.environ`). The same rule applies to any env var read inside a
migration body.

**Rule:** migrations may read `os.environ` directly. Settings is the single
source of truth for _runtime app code_ only (`app/` and below). When a migration
reads an env var that also has a Settings field (e.g. `OPERATOR_USER_ID`), keep
both values in sync at deploy time.
