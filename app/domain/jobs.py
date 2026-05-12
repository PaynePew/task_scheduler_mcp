"""Domain logic for job creation."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.actions.registry import ACTION_REGISTRY
from app.db.models import Job, JobRun, RunEvent


class UnknownActionError(Exception):
    """Raised when action is not registered in ACTION_REGISTRY (maps to UNKNOWN_ACTION)."""


class UnsupportedScheduleTypeError(Exception):
    """Raised when schedule_type is not yet implemented (maps to UNSUPPORTED_SCHEDULE_TYPE).

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
