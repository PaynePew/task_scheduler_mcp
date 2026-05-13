"""Integration tests for app/workers/recurring_watcher.py.

Requires running Postgres (DATABASE_URL set in environment).

Run with:
    uv run pytest -m integration tests/integration/test_recurring_watcher.py
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.engine import create_async_engine
from app.db.models import Job, JobRun, RunEvent
from app.workers.recurring_watcher import PROCESSED_BY_KEY, poll_once

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def session_factory():
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


async def _insert_recurring_job_with_terminal_event(
    factory: async_sessionmaker,
    *,
    cron_expr: str = "0 * * * *",
    event_type: str = "SUCCEEDED",
) -> tuple[Job, JobRun, RunEvent]:
    """Insert a recurring Job + JobRun + terminal RunEvent, committed."""
    scheduled = datetime.now(tz=UTC) - timedelta(hours=1)
    async with factory() as session:
        async with session.begin():
            job = Job(
                user_id="recurring-watcher-test",
                description="recurring echo",
                action="echo",
                action_params={"message": "hi"},
                job_type="recurring",
                scheduled_at=None,
                cron_expr=cron_expr,
            )
            session.add(job)
            await session.flush()

            bucket = scheduled.replace(minute=0, second=0, microsecond=0).isoformat()
            run = JobRun(
                time_bucket=bucket,
                job_id=job.job_id,
                scheduled_at=scheduled,
                status=event_type,
                finish_at=datetime.now(tz=UTC),
            )
            session.add(run)
            await session.flush()

            event = RunEvent(
                run_id=run.run_id,
                job_id=job.job_id,
                event_type=event_type,
                status_from="RUNNING",
                status_to=event_type,
            )
            session.add(event)
    return job, run, event


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_recurring_watcher_stamps_processed_by_and_does_not_insert_run(
    session_factory, caplog
):
    """Terminal event for recurring job → stamped; no new JobRun inserted (W2 TODO)."""
    job, run, event = await _insert_recurring_job_with_terminal_event(session_factory)

    with caplog.at_level(logging.INFO, logger="app.workers.recurring_watcher"):
        count = await poll_once(session_factory)

    assert count == 1

    # processed_by must be stamped
    async with session_factory() as session:
        async with session.begin():
            refreshed = (
                await session.execute(select(RunEvent).where(RunEvent.event_id == event.event_id))
            ).scalar_one()
            # No new JobRun should have been inserted (W2 deferred)
            all_runs = (
                (await session.execute(select(JobRun).where(JobRun.job_id == job.job_id)))
                .scalars()
                .all()
            )

    assert PROCESSED_BY_KEY in refreshed.processed_by
    assert len(all_runs) == 1, "W1 skeleton must NOT insert a second JobRun"

    # Log line confirming intent
    assert any("would have spawned next run" in r.message for r in caplog.records)
    assert any(str(job.job_id) in r.message for r in caplog.records)


@pytest.mark.integration
async def test_recurring_watcher_does_not_reprocess_stamped_event(session_factory):
    """Event already stamped with recurring_watcher key → skipped on second poll."""
    job, run, event = await _insert_recurring_job_with_terminal_event(session_factory)

    # First pass — should process 1 event
    count1 = await poll_once(session_factory)
    assert count1 == 1

    # Second pass — same event is now stamped, must return 0
    count2 = await poll_once(session_factory)
    assert count2 == 0


@pytest.mark.integration
async def test_recurring_watcher_ignores_one_shot_jobs(session_factory):
    """Terminal event for a one_shot job (no cron_expr) → ignored."""
    scheduled = datetime.now(tz=UTC) - timedelta(hours=1)
    async with session_factory() as session:
        async with session.begin():
            job = Job(
                user_id="recurring-watcher-test",
                description="one_shot job",
                action="echo",
                action_params={"message": "hi"},
                job_type="one_shot",
                scheduled_at=scheduled,
            )
            session.add(job)
            await session.flush()

            bucket = scheduled.replace(minute=0, second=0, microsecond=0).isoformat()
            run = JobRun(
                time_bucket=bucket,
                job_id=job.job_id,
                scheduled_at=scheduled,
                status="SUCCEEDED",
                finish_at=datetime.now(tz=UTC),
            )
            session.add(run)
            await session.flush()

            session.add(
                RunEvent(
                    run_id=run.run_id,
                    job_id=job.job_id,
                    event_type="SUCCEEDED",
                    status_from="RUNNING",
                    status_to="SUCCEEDED",
                )
            )

    count = await poll_once(session_factory)
    assert count == 0


@pytest.mark.integration
async def test_recurring_watcher_processes_failed_and_cancelled_events(session_factory, caplog):
    """FAILED and CANCELLED terminal events for recurring jobs are also processed."""
    _, _, failed_event = await _insert_recurring_job_with_terminal_event(
        session_factory, cron_expr="*/5 * * * *", event_type="FAILED"
    )
    _, _, cancelled_event = await _insert_recurring_job_with_terminal_event(
        session_factory, cron_expr="*/10 * * * *", event_type="CANCELLED"
    )

    with caplog.at_level(logging.INFO, logger="app.workers.recurring_watcher"):
        count = await poll_once(session_factory, batch_size=10)

    assert count == 2

    async with session_factory() as session:
        async with session.begin():
            for ev_id in (failed_event.event_id, cancelled_event.event_id):
                refreshed = (
                    await session.execute(select(RunEvent).where(RunEvent.event_id == ev_id))
                ).scalar_one()
                assert PROCESSED_BY_KEY in refreshed.processed_by
