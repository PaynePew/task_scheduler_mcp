"""Unit tests for app/domain/jobs.py — non-DB paths only."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.domain.jobs import (
    InvalidStateError,
    JobNotFoundError,
    UnknownActionError,
    UnsupportedScheduleTypeError,
    cancel_job,
    create_job,
    get_job_with_runs,
)


@pytest.mark.asyncio
async def test_unknown_action_raises_before_db():
    """create_job must raise UnknownActionError for unregistered actions
    without touching the DB (session is never needed if the guard fires first)."""
    with pytest.raises(UnknownActionError, match="not_a_real_action"):
        await create_job(
            None,  # session intentionally None — should never be accessed
            user_id="u1",
            action="not_a_real_action",
            action_params={},
            schedule_type="immediate",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_schedule_type", ["one_shot", "recurring", "scheduled", ""])
async def test_unsupported_schedule_type_raises_before_db(bad_schedule_type):
    """create_job must reject any schedule_type other than 'immediate' before
    touching the DB. Prevents silent degradation to one-shot-now when a future
    slice (S10 future scheduling, S13 recurring) passes an unimplemented value."""
    # match="" would trigger pytest's "always passes" warning; use None to skip
    # message matching for the empty-string case (the exception class is enough).
    match_pattern = bad_schedule_type if bad_schedule_type else None
    with pytest.raises(UnsupportedScheduleTypeError, match=match_pattern):
        await create_job(
            None,  # session intentionally None — should never be accessed
            user_id="u1",
            action="echo",  # valid action so the schedule_type guard is what fires
            action_params={},
            schedule_type=bad_schedule_type,
        )


@pytest.mark.asyncio
async def test_get_job_with_runs_raises_not_found_when_no_job():
    """get_job_with_runs raises JobNotFoundError when the DB returns no matching job."""
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None

    session = AsyncMock()
    session.execute = AsyncMock(return_value=mock_result)

    with pytest.raises(JobNotFoundError):
        await get_job_with_runs(session, user_id="u1", job_id=999, include_runs=False)


def _make_begin_cm():
    """Build a minimal async context manager for session.begin() that propagates exceptions."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _begin():
        yield

    return _begin


@pytest.mark.asyncio
async def test_cancel_job_raises_not_found_when_no_job():
    """cancel_job raises JobNotFoundError when the job doesn't exist or belongs to another user."""
    job_result = MagicMock()
    job_result.scalar_one_or_none.return_value = None

    session = AsyncMock()
    session.execute = AsyncMock(return_value=job_result)
    session.begin = _make_begin_cm()

    with pytest.raises(JobNotFoundError):
        await cancel_job(session, user_id="u1", job_id=999)


@pytest.mark.asyncio
async def test_cancel_job_raises_invalid_state_when_all_runs_terminal():
    """cancel_job raises InvalidStateError when all runs are already in a terminal status."""
    from app.db.models import Job, JobRun

    mock_job = MagicMock(spec=Job)
    mock_job.job_id = 42

    mock_run = MagicMock(spec=JobRun)
    mock_run.status = "SUCCEEDED"

    job_result = MagicMock()
    job_result.scalar_one_or_none.return_value = mock_job

    runs_result = MagicMock()
    runs_result.scalars.return_value.all.return_value = [mock_run]

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[job_result, runs_result])
    session.begin = _make_begin_cm()

    with pytest.raises(InvalidStateError, match="completed"):
        await cancel_job(session, user_id="u1", job_id=42)
