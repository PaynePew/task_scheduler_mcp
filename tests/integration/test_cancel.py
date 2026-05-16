"""Integration tests for task.cancel.v1 — requires running Postgres.

Run with:
    docker compose up -d postgres && alembic upgrade head
    uv run pytest -m integration tests/integration/test_cancel.py
"""

from __future__ import annotations

from datetime import UTC, datetime

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


async def _add_run(
    factory: async_sessionmaker[AsyncSession],
    job_id: int,
    status: str,
) -> int:
    """Insert an extra JobRun for job_id in the given status; return run_id."""
    async with factory() as session:
        async with session.begin():
            now = datetime.now(tz=UTC)
            time_bucket = now.replace(minute=0, second=0, microsecond=0).isoformat()
            result = await session.execute(
                text(
                    "INSERT INTO job_runs (time_bucket, job_id, scheduled_at, status)"
                    " VALUES (:tb, :jid, :sat, :st) RETURNING run_id"
                ),
                {"tb": time_bucket, "jid": job_id, "sat": now, "st": status},
            )
            return result.scalar_one()


# ---------------------------------------------------------------------------
# Existing tests — behaviour preserved from W1 except idempotent re-cancel
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
    """Cancelling a PENDING job writes a RunEvent(CANCELLED) and sets Job.cancelled_at."""
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

            job_result = await session.execute(select(Job).where(Job.job_id == job.job_id))
            db_job = job_result.scalar_one()

    assert all(r.status == "CANCELLED" for r in runs)
    assert len(cancel_events) == 1
    assert cancel_events[0].status_from == "PENDING"
    assert cancel_events[0].status_to == "CANCELLED"
    # AC5: cancelled_at is set on the Job row
    assert db_job.cancelled_at is not None


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
    """Cross-user job_id returns NOT_FOUND (no information leak). AC9."""
    job = await _create_echo_job(session_factory, user_id="owner-user")

    result = await handle_task_cancel(
        {"job_id": job.job_id},
        user_id="attacker-user",
        session_factory=session_factory,
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# New AC tests — best-effort / job-level cancel semantics (ADR-022)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_cancel_already_cancelled_job_is_idempotent(session_factory):
    """Re-cancelling an already-cancelled job is a no-op success. AC8."""
    job = await _create_echo_job(session_factory)

    first = await handle_task_cancel(
        {"job_id": job.job_id},
        user_id="cancel-test-user",
        session_factory=session_factory,
    )
    assert first["ok"] is True

    # Second cancel must also return success (idempotent, not INVALID_STATE).
    result = await handle_task_cancel(
        {"job_id": job.job_id},
        user_id="cancel-test-user",
        session_factory=session_factory,
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "cancelled"


@pytest.mark.integration
async def test_cancel_with_running_and_pending_runs(session_factory):
    """Cancel with one RUNNING + one PENDING run: PENDING flips, RUNNING untouched. AC6."""
    job = await _create_echo_job(session_factory)
    # The job starts with one PENDING run — force it to RUNNING.
    await _force_run_status(session_factory, job.job_id, "RUNNING")
    # Add a second run in PENDING state.
    await _add_run(session_factory, job.job_id, "PENDING")

    result = await handle_task_cancel(
        {"job_id": job.job_id},
        user_id="cancel-test-user",
        session_factory=session_factory,
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "cancelled"

    async with session_factory() as session:
        async with session.begin():
            runs_result = await session.execute(select(JobRun).where(JobRun.job_id == job.job_id))
            runs = runs_result.scalars().all()

            job_result = await session.execute(select(Job).where(Job.job_id == job.job_id))
            db_job = job_result.scalar_one()

            cancel_events_result = await session.execute(
                select(RunEvent).where(
                    RunEvent.job_id == job.job_id, RunEvent.event_type == "CANCELLED"
                )
            )
            cancel_events = cancel_events_result.scalars().all()

    by_status = {r.status: r for r in runs}
    assert "RUNNING" in by_status, "RUNNING run must remain untouched"
    assert "CANCELLED" in by_status, "PENDING run must be flipped to CANCELLED"
    assert db_job.cancelled_at is not None
    # Only the PENDING run should have a CANCELLED event, not the RUNNING one.
    assert len(cancel_events) == 1
    assert cancel_events[0].status_from == "PENDING"


@pytest.mark.integration
async def test_running_run_can_complete_after_cancel(session_factory):
    """RUNNING run writes its natural terminal event after job cancel. AC7."""
    job = await _create_echo_job(session_factory)
    await _force_run_status(session_factory, job.job_id, "RUNNING")

    # Cancel: RUNNING run is left alone, cancelled_at is set.
    cancel_result = await handle_task_cancel(
        {"job_id": job.job_id},
        user_id="cancel-test-user",
        session_factory=session_factory,
    )
    assert cancel_result["ok"] is True

    # Fetch the run_id of the still-RUNNING run.
    async with session_factory() as session:
        async with session.begin():
            run_result = await session.execute(select(JobRun).where(JobRun.job_id == job.job_id))
            run = run_result.scalar_one()
            assert run.status == "RUNNING"
            run_id = run.run_id

    # Simulate the RUNNING run completing naturally (worker writes SUCCEEDED event).
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text("UPDATE job_runs SET status = 'SUCCEEDED' WHERE run_id = :rid"),
                {"rid": run_id},
            )
            session.add(
                RunEvent(
                    run_id=run_id,
                    job_id=job.job_id,
                    event_type="SUCCEEDED",
                    status_from="RUNNING",
                    status_to="SUCCEEDED",
                )
            )

    # The SUCCEEDED event must be present, and cancelled_at must still be set.
    async with session_factory() as session:
        async with session.begin():
            succeeded_events = (
                (
                    await session.execute(
                        select(RunEvent).where(
                            RunEvent.job_id == job.job_id, RunEvent.event_type == "SUCCEEDED"
                        )
                    )
                )
                .scalars()
                .all()
            )

            db_job = (
                await session.execute(select(Job).where(Job.job_id == job.job_id))
            ).scalar_one()

    assert len(succeeded_events) == 1, "Natural SUCCEEDED event must be written"
    assert db_job.cancelled_at is not None, "cancelled_at must not be cleared by run completion"
