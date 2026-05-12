"""Integration tests for task.cancel@v1 — requires running Postgres.

Run with:
    docker compose up -d postgres && alembic upgrade head
    uv run pytest -m integration tests/integration/test_cancel.py
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.engine import create_async_engine
from app.db.models import Job, JobRun, RunEvent
from app.domain.jobs import create_job
from app.mcp.handlers.cancel import handle_task_cancel

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def session_factory() -> async_sessionmaker[AsyncSession]:
    """Fresh engine per test; cleans all job data on teardown."""
    engine = create_async_engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    async with factory() as session:
        async with session.begin():
            await session.execute(text("DELETE FROM run_events"))
            await session.execute(text("DELETE FROM job_runs"))
            await session.execute(text("DELETE FROM jobs"))
    await engine.dispose()


async def _create_echo_job(
    factory: async_sessionmaker[AsyncSession],
    *,
    user_id: str = "cancel-test-user",
) -> Job:
    async with factory() as session:
        return await create_job(
            session,
            user_id=user_id,
            action="echo",
            action_params={"message": "hi"},
            schedule_type="immediate",
        )


async def _force_run_status(
    factory: async_sessionmaker[AsyncSession],
    job_id: int,
    status: str,
) -> None:
    """Directly set all job_runs for a job to a given status (for test setup)."""
    async with factory() as session:
        async with session.begin():
            await session.execute(
                text("UPDATE job_runs SET status = :s WHERE job_id = :jid"),
                {"s": status, "jid": job_id},
            )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_cancel_pending_job_returns_cancelled(session_factory):
    """Cancelling a PENDING job transitions it to CANCELLED and returns external 'cancelled'."""
    job = await _create_echo_job(session_factory)

    result = await handle_task_cancel(
        {"job_id": job.job_id},
        user_id="cancel-test-user",
        session_factory=session_factory,
    )

    assert result["ok"] is True
    assert result["data"]["job_id"] == job.job_id
    assert result["data"]["status"] == "cancelled"


@pytest.mark.integration
async def test_cancel_pending_job_writes_run_event(session_factory):
    """Cancelling a PENDING job writes a RunEvent(CANCELLED) in the same transaction."""
    job = await _create_echo_job(session_factory)

    await handle_task_cancel(
        {"job_id": job.job_id},
        user_id="cancel-test-user",
        session_factory=session_factory,
    )

    async with session_factory() as session:
        async with session.begin():
            run_result = await session.execute(select(JobRun).where(JobRun.job_id == job.job_id))
            runs = run_result.scalars().all()

            event_result = await session.execute(
                select(RunEvent).where(
                    RunEvent.job_id == job.job_id, RunEvent.event_type == "CANCELLED"
                )
            )
            cancel_events = event_result.scalars().all()

    assert all(r.status == "CANCELLED" for r in runs)
    assert len(cancel_events) == 1
    assert cancel_events[0].status_from == "PENDING"
    assert cancel_events[0].status_to == "CANCELLED"


@pytest.mark.integration
async def test_cancel_succeeded_job_returns_invalid_state(session_factory):
    """Cancelling a SUCCEEDED job returns INVALID_STATE."""
    job = await _create_echo_job(session_factory)
    await _force_run_status(session_factory, job.job_id, "SUCCEEDED")

    result = await handle_task_cancel(
        {"job_id": job.job_id},
        user_id="cancel-test-user",
        session_factory=session_factory,
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_STATE"
    assert "completed" in result["error"]["message"]


@pytest.mark.integration
async def test_cancel_failed_job_returns_invalid_state(session_factory):
    """Cancelling a FAILED job returns INVALID_STATE."""
    job = await _create_echo_job(session_factory)
    await _force_run_status(session_factory, job.job_id, "FAILED")

    result = await handle_task_cancel(
        {"job_id": job.job_id},
        user_id="cancel-test-user",
        session_factory=session_factory,
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_STATE"
    assert "failed" in result["error"]["message"]


@pytest.mark.integration
async def test_cancel_already_cancelled_job_returns_invalid_state(session_factory):
    """Cancelling a job that is already CANCELLED returns INVALID_STATE."""
    job = await _create_echo_job(session_factory)
    # First cancel succeeds
    first = await handle_task_cancel(
        {"job_id": job.job_id},
        user_id="cancel-test-user",
        session_factory=session_factory,
    )
    assert first["ok"] is True

    # Second cancel must return INVALID_STATE
    result = await handle_task_cancel(
        {"job_id": job.job_id},
        user_id="cancel-test-user",
        session_factory=session_factory,
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_STATE"
    assert "cancelled" in result["error"]["message"]


@pytest.mark.integration
async def test_cancel_not_found_for_nonexistent_job(session_factory):
    """Nonexistent job_id → NOT_FOUND error."""
    result = await handle_task_cancel(
        {"job_id": 999999999},
        user_id="cancel-test-user",
        session_factory=session_factory,
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "NOT_FOUND"


@pytest.mark.integration
async def test_cancel_not_found_for_cross_user_job(session_factory):
    """Cross-user job_id returns NOT_FOUND (no information leak)."""
    job = await _create_echo_job(session_factory, user_id="owner-user")

    result = await handle_task_cancel(
        {"job_id": job.job_id},
        user_id="attacker-user",
        session_factory=session_factory,
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "NOT_FOUND"
