"""Integration tests for domain.create_job against a real Postgres instance.

Run with:
    docker compose up -d postgres && alembic upgrade head
    uv run pytest -m integration tests/integration/test_create_job.py
"""

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.engine import create_async_engine
from app.db.models import Job, JobRun, RunEvent
from app.domain.jobs import UnknownActionError, create_job


@pytest_asyncio.fixture
async def session_factory():
    """Fresh engine per test — avoids cross-event-loop pool reuse."""
    engine = create_async_engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    # Clean up all test data
    async with factory() as session:
        async with session.begin():
            await session.execute(text("DELETE FROM run_events"))
            await session.execute(text("DELETE FROM job_runs"))
            await session.execute(text("DELETE FROM jobs"))
    await engine.dispose()


@pytest.mark.integration
async def test_immediate_echo_creates_one_job_run_event(session_factory):
    """Creating an immediate echo job inserts 1 Job + 1 JobRun(PENDING) + 1 RunEvent(CREATED)."""
    async with session_factory() as session:
        job = await create_job(
            session,
            user_id="test-user",
            action="echo",
            action_params={"message": "hello"},
            schedule_type="immediate",
        )

    job_id = job.job_id

    async with session_factory() as session:
        async with session.begin():
            job_count = await session.scalar(
                select(func.count()).select_from(Job).where(Job.job_id == job_id)
            )
            runs = (
                (await session.execute(select(JobRun).where(JobRun.job_id == job_id)))
                .scalars()
                .all()
            )
            events = (
                (await session.execute(select(RunEvent).where(RunEvent.job_id == job_id)))
                .scalars()
                .all()
            )

    assert job_count == 1
    assert len(runs) == 1
    assert runs[0].status == "PENDING"
    assert len(events) == 1
    assert events[0].event_type == "CREATED"


@pytest.mark.integration
async def test_idempotency_same_user_returns_existing_job(session_factory):
    """Second call with same (user_id, idempotency_key) returns same job_id; rows unchanged."""
    async with session_factory() as session:
        job1 = await create_job(
            session,
            user_id="idem-user",
            action="echo",
            action_params={"message": "first"},
            schedule_type="immediate",
            idempotency_key="key-abc",
        )

    async with session_factory() as session:
        job2 = await create_job(
            session,
            user_id="idem-user",
            action="echo",
            action_params={"message": "second"},
            schedule_type="immediate",
            idempotency_key="key-abc",
        )

    assert job1.job_id == job2.job_id

    async with session_factory() as session:
        async with session.begin():
            run_count = await session.scalar(
                select(func.count()).select_from(JobRun).where(JobRun.job_id == job1.job_id)
            )
            event_count = await session.scalar(
                select(func.count()).select_from(RunEvent).where(RunEvent.job_id == job1.job_id)
            )

    assert run_count == 1
    assert event_count == 1


@pytest.mark.integration
async def test_different_user_same_idempotency_key_creates_new_job(session_factory):
    """Different user_id + same idempotency_key creates a distinct new Job."""
    async with session_factory() as session:
        job_a = await create_job(
            session,
            user_id="user-A",
            action="echo",
            action_params={"message": "a"},
            schedule_type="immediate",
            idempotency_key="shared-key",
        )

    async with session_factory() as session:
        job_b = await create_job(
            session,
            user_id="user-B",
            action="echo",
            action_params={"message": "b"},
            schedule_type="immediate",
            idempotency_key="shared-key",
        )

    assert job_a.job_id != job_b.job_id


@pytest.mark.integration
async def test_unknown_action_raises_domain_error(session_factory):
    """Unknown action raises UnknownActionError (mappable to UNKNOWN_ACTION)."""
    async with session_factory() as session:
        with pytest.raises(UnknownActionError):
            await create_job(
                session,
                user_id="test-user",
                action="not_registered",
                action_params={},
                schedule_type="immediate",
            )
