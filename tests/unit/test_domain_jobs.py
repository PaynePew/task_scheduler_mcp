"""Unit tests for app/domain/jobs.py — non-DB paths only."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.domain.jobs import (
    InvalidScheduledAtError,
    InvalidStateError,
    InvalidTimezoneError,
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
    """create_job must reject unimplemented schedule_type values before touching the DB.
    'one_shot' (underscore) is distinct from the supported 'one-shot' (hyphen)."""
    match_pattern = bad_schedule_type if bad_schedule_type else None
    with pytest.raises(UnsupportedScheduleTypeError, match=match_pattern):
        await create_job(
            None,  # session intentionally None — should never be accessed
            user_id="u1",
            action="echo",  # valid action so the schedule_type guard is what fires
            action_params={},
            schedule_type=bad_schedule_type,
        )


# ---------------------------------------------------------------------------
# one-shot: timezone validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_timezone",
    ["UTC+8", "+08:00", "Taipei Standard Time", "GMT+5:30", "US/Eastern-notexist"],
)
async def test_one_shot_invalid_timezone_raises_before_db(bad_timezone):
    """Invalid IANA timezone key raises InvalidTimezoneError before DB is touched."""
    future = (datetime.now(tz=UTC) + timedelta(hours=1)).isoformat()
    with pytest.raises(InvalidTimezoneError):
        await create_job(
            None,
            user_id="u1",
            action="echo",
            action_params={},
            schedule_type="one-shot",
            scheduled_at=future,
            timezone=bad_timezone,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "good_timezone", ["UTC", "Asia/Taipei", "Europe/London", "America/New_York"]
)
async def test_one_shot_valid_timezone_passes_validation(good_timezone):
    """Valid IANA timezone keys pass timezone validation; DB call is expected next."""
    future = (datetime.now(tz=UTC) + timedelta(hours=1)).isoformat()
    # session=None → AttributeError when session.begin() is reached, confirming
    # all pre-DB validation passed without raising a domain error.
    with pytest.raises(AttributeError):
        await create_job(
            None,
            user_id="u1",
            action="echo",
            action_params={},
            schedule_type="one-shot",
            scheduled_at=future,
            timezone=good_timezone,
        )


# ---------------------------------------------------------------------------
# one-shot: scheduled_at validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_shot_missing_scheduled_at_raises():
    """one-shot without scheduled_at raises InvalidScheduledAtError before DB."""
    with pytest.raises(InvalidScheduledAtError):
        await create_job(
            None,
            user_id="u1",
            action="echo",
            action_params={},
            schedule_type="one-shot",
            scheduled_at=None,
            timezone="UTC",
        )


@pytest.mark.asyncio
async def test_one_shot_past_scheduled_at_raises():
    """one-shot with a past scheduled_at raises InvalidScheduledAtError before DB."""
    past = (datetime.now(tz=UTC) - timedelta(seconds=10)).isoformat()
    with pytest.raises(InvalidScheduledAtError, match="must be in the future"):
        await create_job(
            None,
            user_id="u1",
            action="echo",
            action_params={},
            schedule_type="one-shot",
            scheduled_at=past,
            timezone="UTC",
        )


@pytest.mark.asyncio
async def test_one_shot_unparseable_scheduled_at_raises():
    """Non-ISO-8601 scheduled_at raises InvalidScheduledAtError before DB."""
    with pytest.raises(InvalidScheduledAtError):
        await create_job(
            None,
            user_id="u1",
            action="echo",
            action_params={},
            schedule_type="one-shot",
            scheduled_at="not-a-date",
            timezone="UTC",
        )


@pytest.mark.asyncio
async def test_one_shot_future_scheduled_at_passes_validation():
    """Valid future scheduled_at passes all pre-DB validation."""
    future = (datetime.now(tz=UTC) + timedelta(hours=2)).isoformat()
    with pytest.raises(AttributeError):
        await create_job(
            None,
            user_id="u1",
            action="echo",
            action_params={},
            schedule_type="one-shot",
            scheduled_at=future,
            timezone="UTC",
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
    mock_job.cancelled_at = None  # not yet cancelled — should reach INVALID_STATE check

    mock_run = MagicMock(spec=JobRun)
    mock_run.status = "SUCCEEDED"

    job_result = MagicMock()
    job_result.scalar_one_or_none.return_value = mock_job

    runs_result = MagicMock()
    runs_result.scalars.return_value.all.return_value = [mock_run]

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[job_result, runs_result])
    session.begin = _make_begin_cm()

    with pytest.raises(InvalidStateError, match="SUCCEEDED"):
        await cancel_job(session, user_id="u1", job_id=42)
