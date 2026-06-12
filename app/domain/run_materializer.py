"""RunMaterializer — single owner of run creation (ADR-065).

Stateless domain module (sibling of chain_validation.py). Both historical
run-creation sites delegate here:

  - ``create_job`` (app/domain/jobs.py) → ``materialize_initial``
  - ``RecurringJobWatcher`` (app/workers/recurring_watcher.py) → ``materialize_successor``

## Interface

``materialize_initial(session, job, *, run_at, wait_run=None) → JobRun``
    First run for a job. Schedule-driven jobs get a PENDING run. Trigger-driven
    jobs get a WAITING run armed against *wait_run* (the upstream's most-recent
    non-terminal run, returned by validate_chain).

``materialize_successor(session, job, *, prev_run, occurred_at) → JobRun``
    Next cron occurrence for a recurring root. Computes run_at via
    next_after(cron_expr, tz, anchor), spawns a PENDING run, then calls
    ``_arm`` to create WAITING downstream runs in the same transaction.

## Internal primitives

``_spawn_run(session, job, *, run_at, status, wait_for_run_id=None) → JobRun``
    The single low-level primitive: insert JobRun + emit CREATED RunEvent +
    derive time_bucket + forbid-concurrency check.  Raises ConcurrencyError
    if an *executing* run already exists for the job.

``_arm(session, upstream_run, *, depth=0) → None``
    For each downstream job (trigger_on_job_id == upstream_run.job_id, not
    cancelled): _spawn_run a WAITING run with wait_for_run_id = upstream_run,
    then recurse (bounded by MAX_CHAIN_DEPTH).  Runs in the same transaction
    as the upstream insert — atomicity is the caller's responsibility.  Arming
    is unconditional per tick: a freshly-armed WAITING run may coexist with the
    previous tick's still-executing (or not-yet-flipped WAITING) downstream run.
    Load-shedding happens later, at flip time (ChainWatcher), as a single audited
    CANCELLED_SLOW_CONSUMER drop — see ADR-065 §4.

Per ADR-033 and ADR-065, ChainWatcher (status coordination) and the executor
(from_run_id injection) are unchanged.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.cron import next_after
from app.db.models import Job, JobRun, RunEvent
from app.domain.chain_validation import MAX_CHAIN_DEPTH

logger = logging.getLogger(__name__)

# Executing statuses: a job may have at most one run in any of these at a time
# (ADR-065 §4). WAITING is deliberately excluded — a WAITING run armed for the
# next tick may coexist with the current tick's executing run; the flip-time
# slow-consumer drop resolves the overlap.
_EXECUTING = frozenset({"PENDING", "QUEUED", "RUNNING", "RETRYING"})


class ConcurrencyError(Exception):
    """Raised by _spawn_run when an executing run already exists for the job.

    ADR-065 §4: at most one *executing* run (PENDING/QUEUED/RUNNING/RETRYING)
    per Job at any time. The caller decides whether to skip or cancel the
    overlapping run.
    """


async def has_executing_run(session: AsyncSession, job_id: int) -> bool:
    """Return True if job_id has at least one *executing* run.

    Executing = PENDING / QUEUED / RUNNING / RETRYING (WAITING excluded). A
    WAITING run armed for the next tick does not count, so it may be created
    while the current tick's run is still in flight (ADR-065 §4).

    Shared predicate for:
    - ``_spawn_run`` / ``RecurringJobWatcher`` (spawn-time forbid-concurrency); and
    - ``ChainWatcher`` (flip-time slow-consumer drop).
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


async def _arm(
    session: AsyncSession,
    upstream_run: JobRun,
    *,
    depth: int = 0,
) -> None:
    """Arm downstream jobs by creating WAITING runs pointing at upstream_run.

    For each active downstream job (trigger_on_job_id == upstream_run.job_id,
    not cancelled), spawn a WAITING run with wait_for_run_id = upstream_run.run_id.
    Then recurse for each newly created run (bounded by MAX_CHAIN_DEPTH).

    Runs inside the caller's transaction (atomic with the upstream insert).
    Arming a WAITING run never raises ConcurrencyError under the executing-only
    invariant (a WAITING run is not an executing run, so it can always be armed).
    The suppression here is defensive: if some future caller arms a non-WAITING
    status into an already-executing job, this tick's arm for that downstream is
    skipped rather than failing the whole materialization.
    """
    if depth >= MAX_CHAIN_DEPTH:
        logger.warning(
            "run_materializer: _arm reached MAX_CHAIN_DEPTH=%d for upstream run_id=%s; stopping",
            MAX_CHAIN_DEPTH,
            upstream_run.run_id,
        )
        return

    # Find all active downstream jobs that trigger on the upstream job.
    downstream_jobs_result = await session.execute(
        select(Job).where(
            Job.trigger_on_job_id == upstream_run.job_id,
            Job.cancelled_at.is_(None),
        )
    )
    downstream_jobs = downstream_jobs_result.scalars().all()

    for downstream in downstream_jobs:
        try:
            waiting_run = await _spawn_run(
                session,
                downstream,
                run_at=upstream_run.scheduled_at,
                status="WAITING",
                wait_for_run_id=upstream_run.run_id,
            )
            logger.info(
                "run_materializer: armed WAITING run_id=%s for downstream job_id=%s"
                " wait_for_run_id=%s (depth=%d)",
                waiting_run.run_id,
                downstream.job_id,
                upstream_run.run_id,
                depth,
            )
            # Recurse for fan-out (depth-bounded).
            await _arm(session, waiting_run, depth=depth + 1)
        except ConcurrencyError:
            logger.info(
                "run_materializer: downstream job_id=%s already has an executing run;"
                " skipping arm for upstream run_id=%s (at-most-one-executing-run)",
                downstream.job_id,
                upstream_run.run_id,
            )


async def materialize_initial(
    session: AsyncSession,
    job: Job,
    *,
    run_at: datetime,
    wait_run: JobRun | None = None,
) -> JobRun:
    """Materialize the first run for a job.

    Schedule-driven (no trigger_on_job_id): creates a PENDING run at run_at.
    Trigger-driven (trigger_on_job_id set): creates a WAITING run at run_at
    with wait_for_run_id = wait_run.run_id.

    Does NOT call _arm — the initial downstream arming is handled by
    validate_chain returning wait_run, which create_job passes here as wait_run.
    The initial run is for the downstream job itself; there is no further arming
    needed at create time (the upstream is already live or running).

    Caller must be inside an open transaction.
    Raises ConcurrencyError if an executing run already exists for the job.
    """
    if wait_run is not None:
        # Trigger-driven: WAITING, armed against the upstream's most-recent run.
        return await _spawn_run(
            session,
            job,
            run_at=run_at,
            status="WAITING",
            wait_for_run_id=wait_run.run_id,
        )
    else:
        # Schedule-driven: PENDING, no upstream dependency.
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

    Then spawns a PENDING root run and calls _arm to create WAITING downstream
    runs in the same transaction.

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

    # Arm downstream jobs in the same transaction (atomic with root run insert).
    await _arm(session, root_run)

    return root_run
