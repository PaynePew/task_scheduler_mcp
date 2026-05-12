"""Integration smoke test: SELECT 1 through the async session factory.

Requires a running Postgres container:
    docker compose up -d postgres && alembic upgrade head
    uv run pytest -m integration
"""

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import async_session_factory


@pytest_asyncio.fixture
async def db_session() -> AsyncSession:
    async with async_session_factory() as session:
        yield session


@pytest.mark.integration
async def test_select_1(db_session: AsyncSession):
    result = await db_session.execute(text("SELECT 1"))
    assert result.scalar() == 1


@pytest.mark.integration
async def test_jobs_table_exists(db_session: AsyncSession):
    result = await db_session.execute(
        text("SELECT table_name FROM information_schema.tables WHERE table_name = 'jobs'")
    )
    assert result.scalar() == "jobs"


@pytest.mark.integration
async def test_job_runs_table_exists(db_session: AsyncSession):
    result = await db_session.execute(
        text("SELECT table_name FROM information_schema.tables WHERE table_name = 'job_runs'")
    )
    assert result.scalar() == "job_runs"


@pytest.mark.integration
async def test_run_events_table_exists(db_session: AsyncSession):
    result = await db_session.execute(
        text("SELECT table_name FROM information_schema.tables WHERE table_name = 'run_events'")
    )
    assert result.scalar() == "run_events"
