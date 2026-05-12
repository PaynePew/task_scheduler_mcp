"""Integration smoke test: SELECT 1 through the async session factory.

Requires a running Postgres container:
    docker compose up -d postgres && alembic upgrade head
    uv run pytest -m integration

Uses the shared `async_session` fixture from `tests/conftest.py`. That
fixture builds a per-test engine to avoid pool reuse across event loops.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.integration
async def test_select_1(async_session: AsyncSession):
    result = await async_session.execute(text("SELECT 1"))
    assert result.scalar() == 1


@pytest.mark.integration
async def test_jobs_table_exists(async_session: AsyncSession):
    result = await async_session.execute(
        text("SELECT table_name FROM information_schema.tables WHERE table_name = 'jobs'")
    )
    assert result.scalar() == "jobs"


@pytest.mark.integration
async def test_job_runs_table_exists(async_session: AsyncSession):
    result = await async_session.execute(
        text("SELECT table_name FROM information_schema.tables WHERE table_name = 'job_runs'")
    )
    assert result.scalar() == "job_runs"


@pytest.mark.integration
async def test_run_events_table_exists(async_session: AsyncSession):
    result = await async_session.execute(
        text("SELECT table_name FROM information_schema.tables WHERE table_name = 'run_events'")
    )
    assert result.scalar() == "run_events"
