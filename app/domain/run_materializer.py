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
    if a non-terminal run already exists for the job.

``_arm(session, upstream_run, *, depth=0) → None``
    For each downstream job (trigger_on_job_id == upstream_run.job_id, not
    cancelled): _spawn_run a WAITING run with wait_for_run_id = upstream_run,
    then recurse (bounded by MAX_CHAIN_DEPTH).  Runs in the same transaction
    as the upstream insert — atomicity is the caller's responsibility.

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

# Non-terminal statuses that block a second spawn for the same job.
_NON_TERMINAL = frozenset({"PENDING", "QUEUED", "WAITING", "RUNNING", "RETRYING"})


class ConcurrencyError(Exception):
    """Raised by _spawn_run when a live run already exists for the job.

    ADR-016 addendum: at most one live JobRun per Job at any time.
    The caller decides whether to skip or cancel the overlapping run.
    """


async def has_live_run(
    session: AsyncSession, job_id: int, *, exclude_run_id: int | None = None
) -> bool:
    """Return True if job_id has at least one non-terminal run.

    When *exclude_run_id* is provided, that specific run is not counted — useful
    when ``ChainWatcher`` wants to know whether a downstream job has a *different*
    live run (i.e. one other than the WAITING run it is currently inspecting).

    Used as the shared predicate for both:
    - ``_spawn_run`` / ``RecurringJobWatcher`` (spawn-time forbid-concurrency); and
    - ``ChainWatcher`` (flip-time slow-consumer drop).
    """
    clause = [
        JobRun.job_id == job_id,
        JobRun.status.in_(list(_NON_TERMINAL)),
    ]
    if exclude_run_id is not None:
        clause.append(JobRun.run_id != exclude_run_id)
    result = await session.execute(select(exists().where(*clause)))
    return bool(result.scalar())


# Internal alias kept for backward-compat callers inside this module.
_has_live_run = has_live_run


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
    Checks forbid-concurrency: raises ConcurrencyError if job already has a
    non-terminal run.

    Caller must be inside an open transaction.
    """
    # Forbid-concurrency: at most one live run per job at any time (ADR-016 addendum).
    if await _has_live_run(session, job.job_id):
        raise ConcurrencyError(
            f"job_id={job.job_id} already has a non-terminal run; cannot spawn {status!r} run"
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
    ConcurrencyError from _spawn_run is logged and suppressed: if the downstream
    already has a live (non-terminal) run, the at-most-one-live-run invariant
    (ADR-016 addendum) forbids spawning a second WAITING run, so this tick's arm
    for that downstream is skipped rather than failing the whole materialization.
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
                "run_materializer: downstream job_id=%s already has a live run;"
                " skipping arm for upstream run_id=%s (at-most-one-live-run)",
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
    Raises ConcurrencyError if a live run already exists for the job.
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
    Raises ConcurrencyError if the root job already has a live run.
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
