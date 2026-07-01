"""RunMaterializer — single owner of run creation (ADR-065, ADR-067).

Stateless domain module (sibling of chain_validation.py). Every run-creation
site delegates here:

  - ``create_job`` (app/domain/jobs.py) → ``materialize_initial`` — the first run
    of a *schedule-driven* job. A trigger-driven (chained) job gets NO initial
    run; its first run is created by the continuation consumer when its upstream
    terminates (ADR-067).
  - the continuation consumer (app/workers/recurring_watcher.py) →
    ``materialize_successor`` (the next recurring tick) and ``materialize_downstream``
    (one trigger-driven run per matching downstream job), both reacting to a
    terminal ``RunEvent``.

## Continuation model (ADR-067)

A downstream run is **created when its upstream run reaches a terminal status**
(continuation), not pre-armed at upstream-creation time. There is no ``WAITING``
limbo and no recursive ``arm`` — one rule, *terminal event → materialise the next
run*, governs recurrence and chaining alike. ``wait_for_run_id`` on a downstream
run carries the **upstream terminal run_id** so the executor injects
``from_run_id`` (the inter-handler data plane, ADR-064).

## Exactly-once (ADR-067 §4)

Creation is idempotent at the data layer via two partial unique indexes on the
run's cause: ``(job_id, wait_for_run_id)`` for trigger-driven runs and
``(job_id, scheduled_at)`` for schedule-driven successors. A redelivered terminal
event that retries a creation raises ``IntegrityError`` on the duplicate insert;
the caller treats it as a no-op. The ``processed_by`` cursor is retained only as
an efficiency layer (skip already-handled events), not the correctness mechanism.

## Slow consumer (ADR-067 §6)

``_spawn_run`` raises ``ConcurrencyError`` when the job already has an *executing*
run (``has_executing_run``). For a downstream creation that means the upstream
outpaced the downstream — the tick is **not created** (skip + audited drop by the
caller), never created-then-cancelled.

## Internal primitives

``_spawn_run(session, job, *, run_at, status, wait_for_run_id=None) → JobRun``
    The single low-level primitive: acquire the per-job advisory lock, check
    forbid-concurrency, insert JobRun + emit CREATED RunEvent + derive time_bucket.
    Raises ConcurrencyError if an *executing* run already exists for the job.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import exists, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.cron import next_after
from app.db.models import Job, JobRun, RunEvent

logger = logging.getLogger(__name__)

# Executing statuses: a job may have at most one run in any of these at a time
# (ADR-065 §4). These are exactly the non-terminal run statuses under the
# continuation model (ADR-067) — the "resident load" set shared by the
# forbid-concurrency check, the slow-consumer skip, and the Job.state settle.
_EXECUTING = frozenset({"PENDING", "QUEUED", "RUNNING", "RETRYING"})


class ConcurrencyError(Exception):
    """Raised by _spawn_run when an executing run already exists for the job.

    ADR-065 §4: at most one *executing* run (PENDING/QUEUED/RUNNING/RETRYING)
    per Job at any time. Under ADR-067 the continuation consumer treats this as
    the slow-consumer skip (do not create), for both recurring successors and
    trigger-driven downstreams.
    """


_ADVISORY_LOCK_SQL = text("SELECT pg_advisory_xact_lock(:job_id)")
"""Transaction-level advisory lock keyed by job_id (issue #237).

Acquired before every check-then-write on the executing-run invariant so that
concurrent sessions cannot both pass the non-locking SELECT and both insert a
second executing run (the classic SELECT-then-act race). The lock is
transaction-scoped: released automatically on COMMIT or ROLLBACK, and re-entrant
within one transaction (the continuation consumer may materialise a successor and
a downstream for the same job under one lock without self-deadlock).
"""


async def _acquire_job_lock(session: AsyncSession, job_id: int) -> None:
    """Acquire a transaction-scoped advisory lock for *job_id*.

    Blocks until the lock is obtained; released automatically on COMMIT or
    ROLLBACK. Callers must be inside an open transaction.
    """
    await session.execute(_ADVISORY_LOCK_SQL, {"job_id": job_id})


async def has_executing_run(session: AsyncSession, job_id: int) -> bool:
    """Return True if job_id has at least one *executing* run.

    Executing = PENDING / QUEUED / RUNNING / RETRYING. Shared predicate for the
    spawn-time forbid-concurrency check and the continuation consumer's
    slow-consumer skip.

    Callers must hold ``_acquire_job_lock(session, job_id)`` before calling this
    function to prevent a non-locking SELECT-then-act race (issue #237).
    """
    result = await session.execute(
        select(
            exists().where(
                JobRun.job_id == job_id,
                JobRun.status.in_(list(_EXECUTING)),
            )
        )
    )
    return bool(result.scalar())


async def _spawn_run(
    session: AsyncSession,
    job: Job,
    *,
    run_at: datetime,
    status: str,
    wait_for_run_id: int | None = None,
) -> JobRun:
    """Low-level primitive: insert JobRun + emit CREATED RunEvent.

    Derives time_bucket from run_at (hour-truncated ISO string, per ADR-009).
    Checks forbid-concurrency: raises ConcurrencyError if job already has an
    executing run.

    Caller must be inside an open transaction.
    """
    # Serialize per job: acquire the advisory lock before the executing-run check
    # so concurrent sessions cannot both pass the SELECT-then-act (issue #237).
    await _acquire_job_lock(session, job.job_id)
    # Forbid-concurrency: at most one executing run per job at any time (ADR-065 §4).
    if await has_executing_run(session, job.job_id):
        raise ConcurrencyError(
            f"job_id={job.job_id} already has an executing run; cannot spawn {status!r} run"
        )

    time_bucket = run_at.replace(minute=0, second=0, microsecond=0).isoformat()

    run = JobRun(
        time_bucket=time_bucket,
        job_id=job.job_id,
        user_id=job.user_id,
        scheduled_at=run_at,
        status=status,
        wait_for_run_id=wait_for_run_id,
    )
    session.add(run)
    await session.flush()  # get run_id

    event = RunEvent(
        run_id=run.run_id,
        job_id=job.job_id,
        event_type="CREATED",
        status_from=None,
        status_to=status,
    )
    session.add(event)

    logger.debug(
        "run_materializer: spawned run_id=%s job_id=%s status=%s scheduled_at=%s",
        run.run_id,
        job.job_id,
        status,
        run_at.isoformat(),
    )
    return run


async def materialize_initial(
    session: AsyncSession,
    job: Job,
    *,
    run_at: datetime,
) -> JobRun:
    """Materialize the first run for a *schedule-driven* job.

    Creates a PENDING run at run_at. Only schedule-driven jobs (no
    ``trigger_on_job_id``) get an initial run: under the continuation model
    (ADR-067) a trigger-driven job has zero runs until its upstream terminates,
    so ``create_job`` does not call this for chained jobs.

    Caller must be inside an open transaction.
    Raises ConcurrencyError if an executing run already exists for the job.
    """
    return await _spawn_run(
        session,
        job,
        run_at=run_at,
        status="PENDING",
        wait_for_run_id=None,
    )


async def materialize_successor(
    session: AsyncSession,
    job: Job,
    *,
    prev_run: JobRun,
    occurred_at: datetime,
) -> JobRun:
    """Materialize the next cron occurrence for a recurring root job.

    Computes run_at = next_after(cron_expr, tz, anchor) where anchor is
    max(occurred_at, prev_run.scheduled_at + 1µs) — the anchor clamp prevents
    re-spawning the same tick when a fast action finishes before its scheduled_at
    (the Watcher's lookahead window can claim runs early; see recurring_watcher.py).

    Then spawns a PENDING root run. No downstream arming: under ADR-067 a chained
    downstream run is created by ``materialize_downstream`` when *this* run later
    terminates, not pre-armed here.

    Caller must be inside an open transaction.
    Raises ConcurrencyError if the root job already has an executing run.
    """
    tz = ZoneInfo(job.timezone or "UTC")
    anchor = max(
        occurred_at,
        prev_run.scheduled_at + timedelta(microseconds=1),
    )
    run_at = next_after(job.cron_expr, tz, anchor)

    root_run = await _spawn_run(
        session,
        job,
        run_at=run_at,
        status="PENDING",
    )

    logger.info(
        "run_materializer: spawned successor run_id=%s for job_id=%s"
        " scheduled_at=%s (cron=%r tz=%s)",
        root_run.run_id,
        job.job_id,
        run_at.isoformat(),
        job.cron_expr,
        job.timezone,
    )

    return root_run


async def materialize_downstream(
    session: AsyncSession,
    *,
    downstream_job: Job,
    upstream_run_id: int,
    occurred_at: datetime,
) -> JobRun:
    """Materialize a trigger-driven downstream run caused by an upstream terminal run.

    Continuation (ADR-067): the downstream run is created PENDING when its
    upstream run reaches a terminal status — never pre-armed. ``wait_for_run_id``
    is set to ``upstream_run_id`` (the upstream terminal run) so the executor
    injects ``from_run_id`` (the data plane, ADR-064), and the
    ``(job_id, wait_for_run_id)`` unique index makes a redelivered create a no-op.
    ``run_at = occurred_at`` so the Watcher picks it up promptly.

    Caller must be inside an open transaction (a savepoint, so a duplicate insert
    can be caught without poisoning the outer transaction).
    Raises ConcurrencyError if the downstream already has an executing run
    (slow consumer → the caller skips with an audited drop, ADR-067 §6).
    """
    return await _spawn_run(
        session,
        downstream_job,
        run_at=occurred_at,
        status="PENDING",
        wait_for_run_id=upstream_run_id,
    )
