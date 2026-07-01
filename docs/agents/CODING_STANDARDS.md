# Coding Standards — chatgpt_task

The canonical coding standard for this repo. Every contributor — human or agent — is expected to follow it, and code review treats it as the single chokepoint for these rules.

Treat each rule as ground truth. A reviewer should refactor the violation (when cheap) or flag it under "Concerns" — never silently accept it.

Each rule is anchored to the ADR / `CONTEXT.md` section that decided it; when this summary and the source disagree, the source wins.

---

## Style — Python 3.12+

- `snake_case` for functions, variables, modules. `PascalCase` for classes, dataclasses, enums, Pydantic models.
- **Type hints on every public function**, including `async def`. Use the built-in generics (`list[int]`, `dict[str, X]`, `X | None`) — never `Optional[X]` or `typing.List`. (Target py312; the project's ruff lint includes `UP`.)
- `from __future__ import annotations` at the top of every new module — keeps forward references cheap and matches existing code.
- Public imports use **absolute paths** rooted at `app.` (e.g. `from app.db.models import Job`). The ruff isort config knows `app` is first-party.
- Prefer `pathlib.Path` over `os.path`.
- Line length **100** chars (ruff is the source of truth — `[tool.ruff] line-length = 100`).
- Naming reads like the domain: `create_job`, `cancel_job`, `claim_run` — verbs that match the CONTEXT.md vocabulary. Never `do_create`, `handle_op`, `process`.

## Style — SQLAlchemy 2.0 async

- All DB code is **async** (`AsyncSession`, `async with session.begin()`, `await session.execute(...)`). No `asyncio.to_thread` bridges — see ADR-011.
- `expire_on_commit=False` is non-negotiable on every session factory (ADR-011). Committed objects must remain usable.
- Sessions are **per-request / per-iteration**, never long-lived. A handler / domain function opens its own session and closes it on the way out.
- Pool sizing per process role is set at the entrypoint (ADR-011 table). Do NOT use defaults in long-running processes.
- **`pool_pre_ping=True` and `pool_recycle=3600` are mandatory** in every `create_async_engine` call. Idle disconnects from RDS / middleboxes are real.
- Eager-load relationships you'll touch after `commit()` via `selectinload(...)` — avoids `DetachedInstanceError` / `MissingGreenlet`.
- Migrations use the **sync psycopg URL** (`ALEMBIC_DATABASE_URL`); the **async asyncpg URL** (`DATABASE_URL`) is runtime-only. They are separate config keys for a reason.

## Style — Pydantic v2 + pydantic-settings

- All config goes through `app.config.settings` (pydantic-settings). No `os.environ.get` scattered through modules — ADR-010 calls config the "single source of truth for env vars".
- Action params each get their own `BaseModel` subclass (`params_model`); action handlers receive validated instances, never raw dicts. (ADR-013)
- Use `field_validator` / `model_validator` for cross-field invariants — not ad-hoc `if` checks inside the handler.

## Tests — pytest

- File names: `tests/unit/test_<module>.py` for pure logic, `tests/integration/test_<feature>.py` for anything that touches DB / SQS / network.
- Every integration test is decorated `@pytest.mark.integration`. Marker is registered in `pyproject.toml` — adding an unmarked DB test makes the unit-only run flake unexpectedly.
- `asyncio_mode = "auto"` is on — do NOT decorate every `async def test_x` with `@pytest.mark.asyncio`. Marker on a per-test basis is only needed when overriding (parametrize, fixture scopes).
- **Fresh engine per test for integration.** Fixture pattern (see `tests/integration/test_cancel.py`):
  ```python
  engine = create_async_engine()
  factory = async_sessionmaker(engine, expire_on_commit=False)
  yield factory
  # teardown: DELETE FROM run_events; DELETE FROM job_runs; DELETE FROM jobs
  await engine.dispose()
  ```
  Reusing a module-level engine across event loops causes `RuntimeError: Event loop is closed` — this bug has already shipped once in this project.
- Test names describe **behaviour**, not implementation: `test_cancel_pending_job_writes_run_event` ✅, `test_cancel_works` ❌.
- One `with pytest.raises(X, match=...)` per error case. `match=None` for unmatchable cases (empty string would trigger the always-passes warning).
- **Do not mock the database in integration tests.** Use the real Postgres from the compose stack (`docker compose --profile full up -d`). Mocked DB tests passed while a prod migration failed — that's the rule's origin.

## Architecture

### Three-layer separation (ADR-010 — strict)

```
mcp/handlers/   →   domain/   →   db/repositories/ (W2: read+write split)
   ↑                  ↑                ↑
   envelope          pure logic       SQLAlchemy queries
   error mapping     domain errors    no business decisions
```

**The domain layer must not import from `app.mcp.*`.** Domain raises domain exceptions (`JobNotFoundError`, `InvalidStateError(internal_status)`, `UnknownActionError`, …) — the handler maps them to envelope codes. This is enforceable by a grep on review: `grep -r "from app.mcp" app/domain/` must be empty.

### MCP tool surface (ADR-014 — strict)

- Every tool name carries `.v1`. Never mutate a `.v1` schema; ship `.v2` alongside.
- Tool input schema: `additionalProperties: false`, every field has `enum`/`default`/`required`. Strict schemas let the LLM self-correct.
- Tool response envelope:
  ```
  success: {"ok": true, "data": {...}}
  error:   {"ok": false, "error": {"code", "message", "field", "expected"}}
  ```
  Error `code` is drawn from the **7-word vocabulary** (ADR-014, amended ADR-060): `USER_INPUT | NOT_FOUND | INVALID_STATE | UNKNOWN_ACTION | DUPLICATE | INTERNAL | MISSING_CONNECTION`. `MISSING_CONNECTION` carries an optional `connect_url` field (required OAuth connection not set up). No new codes without an ADR.
- Datetimes everywhere are **ISO 8601 with explicit timezone**. Default `timezone=UTC` when the LLM client is silent.
- **Internal → external status mapping** happens at the handler boundary, never inside domain or DB code. See CONTEXT.md §2 for the 7→5 table.
- **External status is derived from `(Job.state, latest run)`** via `app.mcp.status_mapping.to_external_job_status` (ADR-067 §9) — never guess a run status in the domain layer (e.g. defaulting a no-run job to `'PENDING'`) and never derive it a second way in a handler. `JobView` / `JobListItem` / `JobResourceItem` carry `job_state` + a nullable `latest_run_status`; the handler calls `to_external_job_status`, not the raw `to_external`, whenever a job may have zero runs. `task.status.v1` also surfaces `triggered_by: <job_id>` for a trigger-driven job (`Job.trigger_on_job_id`) — see CONTEXT.md §2.

### Outbox pattern (ADR-009 — strict)

Every status transition writes a `RunEvent` in the **same transaction** as the `job_runs.status` update. Wrap both writes in one `async with session.begin():` block.

The downstream consumer (the single **continuation consumer**) reads **`run_events`**, never `job_runs.status` directly. The status column is mutable; the event log is the immutable source of truth.

### Run creation & chaining (ADR-065 + ADR-067 — strict)

- **Single owner of run creation.** All `JobRun` creation goes through the `RunMaterializer` domain module — `materialize_initial` (a schedule-driven job's first run), `materialize_successor` (the next recurring tick), `materialize_downstream` (a trigger-driven downstream run). Do NOT hand-roll `JobRun` + `RunEvent` inserts in `create_job`, the continuation consumer, or elsewhere — scattered run-creation is the bug ADR-065 fixed.
- **Continuation, not pre-arm (ADR-067).** A trigger-driven downstream run is **created when its upstream run reaches a terminal status**, by the single continuation consumer (the generalised `RecurringJobWatcher`) — never pre-armed as a `WAITING` run at upstream-creation time. `trigger_on_status` is a **create predicate** (create the downstream iff the upstream terminal status matches); a predicate miss creates no run and records a lightweight audit event. The `WAITING` status and `ChainWatcher` are **removed** (ADR-067; migration 0012) — re-introducing pre-armed `WAITING` runs, a `ChainWatcher`, or an `arm`/`re-arm` model is a review block.
- **Trigger-driven `Job.state` settle (ADR-068 §2).** A chained job settles `active → completed` when its trigger parent is terminal (`completed`/`cancelled`) **and** it has no non-terminal run — one-hop parent propagation driven by the continuation consumer via `settle_check`, which cascades along the chain; cancelling a job cascades the settle to its downstreams. The executor's `settle_job` stays the non-cascading one-shot/immediate self-settle; do NOT make it cascade (it runs before the downstream run is materialised, so cascading there would prematurely settle a downstream that is about to fire).
- **Exactly-once at the data layer (ADR-067 §4).** Run creation is idempotent via partial unique indexes on the run's cause — `(job_id, wait_for_run_id)` for trigger-driven runs, `(job_id, scheduled_at)` for schedule-driven successors. A redelivered terminal event that retries a create is a no-op; the `processed_by` cursor is retained **only** as an efficiency layer, not the correctness mechanism. Terminal events are processed in `event_id` order.
- **One executing run per `Job`; slow consumer = skip-create.** At most one *executing* `JobRun` per `Job` — forbid-concurrency via `has_executing_run` (`PENDING`/`QUEUED`/`RUNNING`/`RETRYING`). When the downstream already has an executing run, the overlapping tick is **not created** (skip + audited drop), never created-then-cancelled and never run concurrently or queued.

### Action registry (ADR-013)

- One handler class per action, registered in `app.actions.registry.ACTION_REGISTRY` by string name.
- Per-handler `params_model: ClassVar[type[BaseModel]]` and `timeout_seconds: ClassVar[int]`. `asyncio.wait_for(handler.execute(...), timeout=handler.timeout_seconds)` is enforced at the worker level — never inside the handler.
- `ActionResult.retryable=False` means **permanent failure → DLQ on next delete**. Use it for 4xx-style errors the user caused; reserve `retryable=True` for transient infrastructure failures.
- **Every handler declares `idempotent: ClassVar[bool]` (ADR-013 amendment, issue #268).** No default — `ActionHandler` is a `Protocol`, so a new handler that omits it is a bug, not a silent fallback. Pure/output-only actions (no external effect: `echo`, `llm_summarize`, `llm_polish`, `calendar_digest_ics`) are `True`; anything with an external side effect (`email_send`, `slack_post`, `github_digest`, `http_call`) is `False`. Add the new action to `tests/unit/test_action_idempotency.py`'s `EXPECTED_IDEMPOTENT_POSTURE` map in the same change — that test fails closed on any registry/map mismatch. This flag is the shared input to the reconciler's `RUNNING`-orphan recovery (retry-in-place vs fail-and-alert, PRD #266) — do not hardcode an action-name check where this flag belongs.

### Watcher / Worker safety (ADR-007, ADR-008)

- The Watcher's claim query uses `FOR UPDATE SKIP LOCKED`. Multiple watcher instances must remain safe — never introduce a non-locking path.
- The Worker's claim is `UPDATE job_runs SET status='RUNNING' WHERE run_id=:rid AND status IN ('PENDING','QUEUED','RETRYING') RETURNING ...`. Atomic single-statement, no SELECT-then-UPDATE as the guard. `RETRYING` is in the accepted set on purpose: a redelivery after a retryable failure must be re-claimable, else the message would be deleted as if already-processed. (`app/workers/executor.py:_claim`.)
- Long-running actions extend SQS visibility via `heartbeat` (every 30s). A crashed worker's message must be allowed to fail over to another worker — never delete a message before the handler returns success. ⚠ Known gap (not yet a rule): the heartbeat renews only the SQS visibility, not any DB-side lease, so a hard crash *after* the claim (row=`RUNNING`) *before* terminal leaves an orphaned `RUNNING` row that the redelivered message cannot re-claim (claim rejects `RUNNING`) and that the reconciler does not yet sweep. Recovery of `RUNNING` orphans is tracked as pending work — do not assume it exists today.

### Process roles & entrypoints

Each entrypoint is **~10 lines**: import the loop / server, wire config, run. Business logic lives in `app.domain.*` / `app.workers.*`, not in entrypoints. If an entrypoint grows past 30 lines, the logic is in the wrong place.

## Domain language (CONTEXT.md is the source of truth)

- **`Job` ≠ `JobRun` ≠ `RunEvent`.** Most bugs in this project come from confusing them. Review every change for vocabulary drift:
  - `Job` — the schedule definition. Mutable. One row per scheduled task.
  - `JobRun` — one execution attempt. Limited mutability (status transitions only). One row per attempt.
  - `RunEvent` — append-only state-transition record. Never mutate.
- **`Tool` ≠ `Action`.** `Tool` is the MCP surface (`task.create.v1`); `Action` is what the worker dispatches (`echo`, `http_call`). A new action is one registry entry, not a new tool.
- **`schedule_type`** is one of `immediate | one-shot | recurring` — **all three are supported** (recurring + one-shot run in prod). The W1-only `UnsupportedScheduleTypeError` restriction is obsolete — do NOT reintroduce it or flag recurring/one-shot code as unsupported.
- **Run source (ADR-065).** Every `JobRun` has exactly one run source: **schedule-driven** (`cron_expr` / `scheduled_at`) **XOR** **trigger-driven** (`trigger_on_job_id`), mutually exclusive per `Job`. A chained job carries **no `cron_expr`** — recurrence is *inherited* from its trigger. `trigger_on_job_id` + `cron_expr` together → rejected at create time (`USER_INPUT`, rule V6).
- **Run-source vocabulary (CONTEXT.md §7).** `RunMaterializer` (single owner of run creation), `continuation` (a downstream run is created when its upstream run terminates — replaces the old pre-armed `arm` / `WAITING` model, ADR-067), `create predicate` (`trigger_on_status` applied at create time), `inherited recurrence`, `fan-out` (one upstream → many downstream — allowed) vs `fan-in` (one downstream ← many upstreams, `from_run_ids` — deferred, ADR-040). Reject drift from these terms.
- New terminology = update CONTEXT.md in the same branch. The reviewer must reject vocabulary drift even if the code works.

## Anti-patterns (real bugs from this project — do not repeat)

1. **Reading ORM attributes AFTER `session.execute(update(...))`.** SQLAlchemy 2.x bulk update defaults to `synchronize_session="auto"`, which mutates the in-memory instance. Capture pre-update values into a local variable BEFORE the UPDATE. (Issue #10 root cause — see `cancel_job` history.)

2. **Module-level engines reused across event loops.** Tests using a shared module-level `create_async_engine()` pass when one test runs in isolation but fail with `RuntimeError: Event loop is closed` under the full suite. Always use a per-test fixture.

3. **`postgresql.TIMESTAMPTZ()`** — this is not a SQLAlchemy attribute, even though the type name exists in PostgreSQL. Use `postgresql.TIMESTAMP(timezone=True)`. (Issue #1.)

4. **Domain raising MCP envelope errors.** `app/domain/` must never construct `{"ok": false, "error": {...}}`. It raises typed exceptions; the handler does the formatting. (ADR-010, enforced by grep.)

5. **Reading `job_runs.status` from a downstream watcher.** Watchers must consume `run_events`. The mutable status column races with retries; the immutable event log doesn't. (ADR-009.)

6. **Mutating a `.v1` tool's input schema.** Breaks every long-running MCP thread. Always ship `.v2` alongside. (ADR-014.)

7. **Silent integration-test skips.** A `pip install pytest` fallback + `-m "not integration"` is not a passing test run. If the venv or DB is unreachable, report BLOCKED — do not redefine COMPLETE. (Issue #10.)

8. **Hardcoding the DB hostname in tests.** Read the connection string from `DATABASE_URL` (already set in the environment) — never hardcode `postgres` or `localhost`. A container without a docker socket reaches the host's Postgres via `host.docker.internal:5432`, not via the compose service name `postgres`; a hardcoded hostname passes in one environment and silently fails in the other.

9. **Forwarding ElasticMQ-synthesised receipt handles to the real ElasticMQ.** A previous test fixture round-tripped synthetic receipts and got `ReceiptHandleIsInvalid`. If you need a fake receipt for a unit test, keep it in the test boundary; do not call `delete_message` with it.

10. **A test that fakes the very seam it claims to exercise.** `test_chain_recurring` built downstream runs by hand (`_insert_downstream_run` pre-set `wait_for_run_id`) while claiming to test recurring chains — so the suite was green while the real spawn path never ran. A faked-seam test is worse than no test: it disguises *unverified* as *verified*. If a test mocks or fakes the exact mechanism under test, rewrite it to drive the real path — now the continuation consumer → `materialize_downstream` path. (ADR-065 / #224; rewritten for continuation in ADR-067 / #254.)

## Commits

- Conventional Commits: `feat | fix | refactor | test | docs | chore | perf | ci`.
- Subject ≤ 72 chars, imperative mood. **Scope in parens** when meaningful: `feat(domain):`, `fix(mcp):`, `refactor(tests):`.
- Body explains **why**, not what (the diff shows what). When fixing a behavioural bug, name the surprising mechanism in one sentence (e.g. "SQLAlchemy synchronize_session='auto' mutates in-memory ORM instances after bulk update").
- One logical change per commit. The reviewer's "Changes made" section is one bullet per commit — splitting is friction-free, batching is hostile.
- Reference the issue with `Refs #N` or `Closes #N` in the body when the commit is part of an issue's slice.

## What NOT to do

- No `print()` for debugging — use `logging.getLogger(__name__)`. Stray `print()` is a review block.
- No bare `except:` or `except Exception:` without a `logger.exception(...)` + re-raise or a documented reason.
- No `# type: ignore` without a same-line comment explaining why. Same for `# noqa`. (Project doesn't run mypy yet per ADR-002, but the discipline matters.)
- No hardcoded credentials / hostnames / ports. Read from `app.config.settings`.
- No `time.sleep()` in async code — use `asyncio.sleep()`. Same for `requests` — use `httpx.AsyncClient`.
- No nested ternaries. Use `if/else` chains or extract a named helper.
- No `from app.mcp.* import ...` inside `app/domain/*`. (ADR-010 layer violation; grep-enforceable.)
- No mutating `.v1` MCP schemas. (ADR-014 — schema change ⇒ new version suffix.)
- No new module-level mutable state in long-running processes — pass it explicitly or scope it to the session/task.

## Verification cheatsheet

Before marking a change COMPLETE, run and cite the exit code of:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest -m "not integration"
uv run pytest -m integration
```

The integration suite needs Postgres + ElasticMQ up (`docker compose --profile full up -d`). If it fails with `ECONNREFUSED`, the stack isn't reachable — report BLOCKED, do not silently skip. `DATABASE_URL`, `ALEMBIC_DATABASE_URL`, `QUEUE_URL` must point at the running services (`host.docker.internal:5432` / `:9324` when tests run inside a container).
