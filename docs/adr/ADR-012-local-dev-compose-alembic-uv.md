# ADR-012: Local dev — dual-profile Docker Compose + Alembic + uv + multi-entrypoint image

- **Status**: Accepted
- **Date**: 2026-05-12
- **Source**: doc/session/grilling-state.md Q12
- **Related**: ADR-002 (Python), ADR-010 (module layout)

## Context

The local dev loop must support: (1) fast iteration (Python on host with hot reload), (2) integration testing against the full stack in containers, (3) one-shot Alembic migrations before app services start, (4) easy environment-variable management without leaking secrets.

## Decision

**Docker Compose with two profiles:**

- **Default (infra only)** — `postgres` + `elasticmq` + `migrate`. Developer runs Python on the host for fast iteration.
- **`--profile full`** — adds `mcp-server`, `watcher`, `worker`, `recurring-watcher`, `chain-watcher`. Mirrors W3 production-like behaviour for integration tests.

**Alembic** for migrations:

- Sync `psycopg` URL (see ADR-011).
- `migrate` service uses Compose's `service_completed_successfully` condition; app services depend on it. No manual migration step.

**`uv`** for dependency management (Astral, Rust-based — 10–100× faster than pip). `pyproject.toml` is the source of truth; `uv.lock` is committed.

**`.env.example` committed; `.env` gitignored.** Pydantic-settings reads `.env` for typed env-var contract.

**One Dockerfile, six entrypoints.** Each Compose service uses the same image with a different `command:` value (e.g. `python -m app.entrypoints.mcp_stdio`).

## Alternatives considered

- **Single profile** — slows the dev loop because every Python change rebuilds containers.
- **`docker-compose run migrate` as a manual step** — easy to forget; CI/CD would re-implement the dependency.
- **pip / Poetry** — slower; modern Python signal favours `uv`.
- **Per-service Dockerfile** — multiplies build time; duplicates dependency graphs.

## Consequences

- `docker compose up` brings up infra in seconds; the dev loop is "edit Python, save, re-run".
- `docker compose --profile full up` is the integration-test command and the W3 dress rehearsal.
- The same image (one Dockerfile) maps 1:1 to W3 ECS task definitions — only the `command:` differs.
- A new developer needs `docker`, `uv`, and `.env.example` → working in under a minute.
- `.env` is intentionally gitignored; secrets never enter the repo.
