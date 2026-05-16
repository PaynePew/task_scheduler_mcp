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
    timezone: str = "UTC",
    cancelled_at: datetime | None = None,
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
                timezone=timezone,
                cancelled_at=cancelled_at,
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
                occurred_at=scheduled + timedelta(minutes=30),
            )
            session.add(event)
    return job, run, event


# ---------------------------------------------------------------------------
# Core spawn behaviour
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_tick_spawns_exactly_one_next_run(session_factory):
    """Terminal event for active recurring job → exactly one PENDING JobRun spawned."""
    job, run, event = await _insert_recurring_job_with_terminal_event(
        session_factory,
        cron_expr="0 * * * *",  # every hour
        timezone="UTC",
    )

    count = await poll_once(session_factory)
    assert count == 1

    async with session_factory() as session:
        async with session.begin():
            all_runs = (
                (await session.execute(select(JobRun).where(JobRun.job_id == job.job_id)))
                .scalars()
                .all()
            )
            refreshed_event = (
                await session.execute(select(RunEvent).where(RunEvent.event_id == event.event_id))
            ).scalar_one()

    # Exactly one NEW run was inserted (plus the original one = 2 total)
    assert len(all_runs) == 2, f"Expected 2 runs, got {len(all_runs)}"
    new_run = next(r for r in all_runs if r.run_id != run.run_id)
    assert new_run.status == "PENDING"

    # scheduled_at must be strictly after the terminal event's occurred_at
    assert new_run.scheduled_at > event.occurred_at

    # processed_by must be stamped
    assert PROCESSED_BY_KEY in refreshed_event.processed_by


@pytest.mark.integration
async def test_tick_scheduled_at_matches_cron_next(session_factory):
    """Spawned JobRun.scheduled_at equals next_after(cron_expr, tz, occurred_at)."""
    from zoneinfo import ZoneInfo

    from app.config.cron import next_after

    occurred_at = datetime(2026, 1, 15, 8, 30, 0, tzinfo=UTC)  # fixed time for determinism
    async with session_factory() as session:
        async with session.begin():
            job = Job(
                user_id="recurring-watcher-test",
                description="recurring echo",
                action="echo",
                action_params={"message": "hi"},
                job_type="recurring",
                scheduled_at=None,
                cron_expr="0 9 * * *",  # daily at 9 AM UTC
                timezone="UTC",
            )
            session.add(job)
            await session.flush()
            bucket = occurred_at.replace(minute=0, second=0, microsecond=0).isoformat()
            run = JobRun(
                time_bucket=bucket, job_id=job.job_id, scheduled_at=occurred_at, status="SUCCEEDED"
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
                    occurred_at=occurred_at,
                )
            )

    await poll_once(session_factory)

    async with session_factory() as session:
        async with session.begin():
            all_runs = (
                (await session.execute(select(JobRun).where(JobRun.job_id == job.job_id)))
                .scalars()
                .all()
            )

    new_run = next(r for r in all_runs if r.run_id != run.run_id)
    expected_at = next_after("0 9 * * *", ZoneInfo("UTC"), occurred_at)
    assert new_run.scheduled_at == expected_at, (
        f"scheduled_at {new_run.scheduled_at} != expected {expected_at}"
    )


# ---------------------------------------------------------------------------
# Cancelled job: no spawn
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_cancelled_job_does_not_spawn_next_run(session_factory):
    """Job with cancelled_at set → terminal event → tick → NO new JobRun spawned."""
    job, run, event = await _insert_recurring_job_with_terminal_event(
        session_factory,
        cron_expr="0 * * * *",
        cancelled_at=datetime.now(tz=UTC),
    )

    count = await poll_once(session_factory)
    assert count == 1  # event was processed (stamped)

    async with session_factory() as session:
        async with session.begin():
            all_runs = (
                (await session.execute(select(JobRun).where(JobRun.job_id == job.job_id)))
                .scalars()
                .all()
            )
            refreshed_event = (
                await session.execute(select(RunEvent).where(RunEvent.event_id == event.event_id))
            ).scalar_one()

    # No new run was inserted
    assert len(all_runs) == 1, "Cancelled job must not spawn a next run"
    # Event was still stamped
    assert PROCESSED_BY_KEY in refreshed_event.processed_by


# ---------------------------------------------------------------------------
# Idempotency: calling tick twice processes each event exactly once
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_idempotent_tick_processes_each_event_once(session_factory):
    """Event already stamped with processed_by key → skipped on second poll."""
    job, run, event = await _insert_recurring_job_with_terminal_event(session_factory)

    # First tick → processes 1 event, inserts 1 run
    count1 = await poll_once(session_factory)
    assert count1 == 1

    # Second tick → same event is stamped, must return 0 and not insert another run
    count2 = await poll_once(session_factory)
    assert count2 == 0

    async with session_factory() as session:
        async with session.begin():
            all_runs = (
                (await session.execute(select(JobRun).where(JobRun.job_id == job.job_id)))
                .scalars()
                .all()
            )

    assert len(all_runs) == 2, "Two ticks must not produce more than 2 runs (1 original + 1 next)"


# ---------------------------------------------------------------------------
# Legacy filter tests (still valid)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_recurring_watcher_does_not_reprocess_stamped_event(session_factory):
    """Event already stamped with recurring_watcher key → skipped on second poll."""
    _, run, event = await _insert_recurring_job_with_terminal_event(session_factory)

    count1 = await poll_once(session_factory)
    assert count1 == 1

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
    """FAILED and CANCELLED terminal events for non-cancelled recurring jobs are processed."""
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
