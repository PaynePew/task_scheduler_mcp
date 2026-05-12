"""Unit tests for app/domain/jobs.py — non-DB paths only."""

import pytest

from app.domain.jobs import UnknownActionError, create_job


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
