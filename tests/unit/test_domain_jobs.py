"""Unit tests for app/domain/jobs.py — non-DB paths only."""

import pytest

from app.domain.jobs import (
    UnknownActionError,
    UnsupportedScheduleTypeError,
    create_job,
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
    with pytest.raises(UnsupportedScheduleTypeError, match=bad_schedule_type if bad_schedule_type else ""):
        await create_job(
            None,  # session intentionally None — should never be accessed
            user_id="u1",
            action="echo",  # valid action so the schedule_type guard is what fires
            action_params={},
            schedule_type=bad_schedule_type,
        )
