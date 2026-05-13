"""ChatGPT Task Scheduler — MCP-based job scheduler (Postgres + SQS).

New here? Read these first, in order:
  1. ``CONTEXT.md`` (repo root) — domain glossary. Trust this over the PRD.
  2. ``docs/PRD/prototype-w1.md`` — the W1 scope.
  3. ``docs/adr/`` — one ADR per architectural decision, numbered.

Process map (six entrypoints, one image — see ADR-010 / CONTEXT.md §4):

    mcp-server   handles MCP tool calls (stdio or HTTP)
    watcher      claims due ``PENDING`` runs → SQS    (multi-instance, FOR UPDATE SKIP LOCKED)
    worker       pulls SQS → dispatches Action handler
    recurring    spawns next ``JobRun`` from terminal events  (W2 logic; skeleton only)
    chain        flips ``WAITING`` → ``PENDING`` from terminal events  (W2 logic; skeleton only)
    migrate      one-shot: ``alembic upgrade head`` before services start

Package map:

    app/config/        env-var settings (pydantic-settings)
    app/db/            engine, ORM models, identity resolver
    app/domain/        business logic; *knows nothing about MCP* (ADR-010)
    app/mcp/           tool schemas, handlers, envelope, error mapping
    app/workers/       watcher + worker loop bodies
    app/queue/         SQS client wrapper (ElasticMQ locally, SQS in prod)
    app/actions/       action handlers + registry (echo, http_call, ...)
    app/entrypoints/   ~10-line ``python -m`` targets, one per process

Key invariants — break these and reliability breaks:
  - Every status transition writes a ``RunEvent`` in the **same** transaction
    as the ``job_runs.status`` update. This is the transactional outbox; downstream
    watchers consume the immutable event log, never the mutable status column.
  - Worker claim is atomic: ``UPDATE ... WHERE status IN ('PENDING','QUEUED')
    RETURNING ...``. Only one worker wins on duplicate SQS delivery.
  - The watcher's claim uses ``FOR UPDATE SKIP LOCKED`` so N watcher processes
    cooperate without leader election (ADR-007).
"""
