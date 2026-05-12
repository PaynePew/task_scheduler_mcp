"""Domain logic for job operations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.actions.registry import ACTION_REGISTRY
from app.db.models import Job, JobRun, RunEvent


class UnknownActionError(Exception):
    """Raised when action is not registered in ACTION_REGISTRY (maps to UNKNOWN_ACTION)."""


class JobNotFoundError(Exception):
    """Raised when job_id does not exist or belongs to another user (maps to NOT_FOUND)."""


class UnsupportedScheduleTypeError(Exception):
    """Raised when schedule_type is not yet implemented (maps to USER_INPUT).

    S04 only implements 'immediate'. One-shot-with-future-datetime and
    recurring/chain schedules land in later slices (S10, S13). Until then,
    passing anything other than 'immediate' is rejected explicitly rather
    than silently degraded to one-shot-now (which would mask scheduling bugs
    far from their cause).
    """


async def create_job(
    session: AsyncSession,
    *,
    user_id: str,
    action: str,
    action_params: dict,
    schedule_type: str,
    idempotency_key: str | None = None,
) -> Job:
    """Insert Job + JobRun(PENDING) + RunEvent(CREATED) in one transaction.

    Returns the existing Job on (user_id, idempotency_key) collision.
    Raises UnknownActionError if action is not in ACTION_REGISTRY.
    Raises UnsupportedScheduleTypeError for any schedule_type other than 'immediate'.
    """
    if schedule_type != "immediate":
        raise UnsupportedScheduleTypeError(schedule_type)
    if action not in ACTION_REGISTRY:
        raise UnknownActionError(action)

    async with session.begin():
        if idempotency_key is not None:
            result = await session.execute(
                select(Job).where(
                    Job.idempotency_key == idempotency_key,
                    Job.user_id == user_id,
                )
            )
            existing = result.scalar_one_or_none()
            if existing is not None:
                return existing

        now = datetime.now(tz=UTC)
        time_bucket = now.replace(minute=0, second=0, microsecond=0).isoformat()

        job = Job(
            user_id=user_id,
            description=action,
            action=action,
            action_params=action_params,
            job_type="one_shot",
            scheduled_at=now,
            idempotency_key=idempotency_key,
        )
        session.add(job)
        await session.flush()

        run = JobRun(
            time_bucket=time_bucket,
            job_id=job.job_id,
            scheduled_at=now,
            status="PENDING",
        )
        session.add(run)
        await session.flush()

        event = RunEvent(
            run_id=run.run_id,
            job_id=job.job_id,
            event_type="CREATED",
        )
        session.add(event)

    return job


@dataclass
class RunView:
    run_id: int
    status: str
    scheduled_at: datetime
    start_at: datetime | None
    finish_at: datetime | None


@dataclass
class JobView:
    job_id: int
    action: str
    internal_status: str
    runs: list[RunView] | None


async def get_job_with_runs(
    session: AsyncSession,
    *,
    user_id: str,
    job_id: int,
    include_runs: bool,
) -> JobView:
    """Return the job's current status and optionally its 10 most recent runs.

    Raises JobNotFoundError if job_id does not exist or belongs to another user.
    The most recent 10 runs are ordered newest-first by start_at, falling back
    to scheduled_at when start_at is NULL.
    """
    job_result = await session.execute(
        select(Job).where(Job.job_id == job_id, Job.user_id == user_id)
    )
    job = job_result.scalar_one_or_none()
    if job is None:
        raise JobNotFoundError(job_id)

    sort_key = func.coalesce(JobRun.start_at, JobRun.scheduled_at).desc()
    limit = 10 if include_runs else 1
    runs_result = await session.execute(
        select(JobRun).where(JobRun.job_id == job_id).order_by(sort_key).limit(limit)
    )
    db_runs = runs_result.scalars().all()

    internal_status = db_runs[0].status if db_runs else "PENDING"
    run_views = [
        RunView(
            run_id=r.run_id,
            status=r.status,
            scheduled_at=r.scheduled_at,
            start_at=r.start_at,
            finish_at=r.finish_at,
        )
        for r in db_runs
    ]

    return JobView(
        job_id=job.job_id,
        action=job.action,
        internal_status=internal_status,
        runs=run_views if include_runs else None,
    )
