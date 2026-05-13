"""Integration tests for app/workers/chain_watcher.py.

Requires running Postgres (DATABASE_URL set in environment).

Run with:
    uv run pytest -m integration tests/integration/test_chain_watcher.py
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
from app.workers.chain_watcher import PROCESSED_BY_KEY, poll_once

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


async def _insert_trigger_chain(
    factory: async_sessionmaker,
    *,
    event_type: str = "SUCCEEDED",
    trigger_on_status: str = "SUCCEEDED",
) -> tuple[Job, Job, JobRun, JobRun, RunEvent]:
    """Insert upstream job + downstream chained job, plus a terminal RunEvent for upstream.

    Returns (upstream_job, downstream_job, upstream_run, downstream_run, terminal_event).
    """
    scheduled = datetime.now(tz=UTC) - timedelta(hours=1)
    async with factory() as session:
        async with session.begin():
            upstream_job = Job(
                user_id="chain-watcher-test",
                description="upstream job",
                action="echo",
                action_params={"message": "upstream"},
                job_type="one_shot",
                scheduled_at=scheduled,
            )
            session.add(upstream_job)
            await session.flush()

            bucket = scheduled.replace(minute=0, second=0, microsecond=0).isoformat()
            upstream_run = JobRun(
                time_bucket=bucket,
                job_id=upstream_job.job_id,
                scheduled_at=scheduled,
                status=event_type,
                finish_at=datetime.now(tz=UTC),
            )
            session.add(upstream_run)
            await session.flush()

            # Downstream job triggered by upstream
            downstream_job = Job(
                user_id="chain-watcher-test",
                description="downstream job",
                action="echo",
                action_params={"message": "downstream"},
                job_type="one_shot",
                scheduled_at=scheduled + timedelta(seconds=1),
                trigger_on_job_id=upstream_job.job_id,
                trigger_on_status=trigger_on_status,
            )
            session.add(downstream_job)
            await session.flush()

            downstream_run = JobRun(
                time_bucket=bucket,
                job_id=downstream_job.job_id,
                scheduled_at=scheduled + timedelta(seconds=1),
                status="WAITING",
                wait_for_run_id=upstream_run.run_id,
            )
            session.add(downstream_run)
            await session.flush()

            # Terminal event for upstream
            event = RunEvent(
                run_id=upstream_run.run_id,
                job_id=upstream_job.job_id,
                event_type=event_type,
                status_from="RUNNING",
                status_to=event_type,
            )
            session.add(event)

    return upstream_job, downstream_job, upstream_run, downstream_run, event


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_chain_watcher_stamps_processed_by_and_does_not_flip_waiting(session_factory, caplog):
    """Terminal event for upstream job → stamped; WAITING run stays WAITING (W2 TODO)."""
    upstream_job, downstream_job, upstream_run, downstream_run, event = await _insert_trigger_chain(
        session_factory
    )

    with caplog.at_level(logging.INFO, logger="app.workers.chain_watcher"):
        count = await poll_once(session_factory)

    assert count == 1

    async with session_factory() as session:
        async with session.begin():
            refreshed_event = (
                await session.execute(select(RunEvent).where(RunEvent.event_id == event.event_id))
            ).scalar_one()
            downstream = (
                await session.execute(select(JobRun).where(JobRun.run_id == downstream_run.run_id))
            ).scalar_one()

    assert PROCESSED_BY_KEY in refreshed_event.processed_by
    assert downstream.status == "WAITING", "W1 skeleton must NOT flip WAITING runs"

    # Log line confirming intent
    assert any("would have flipped WAITING runs" in r.message for r in caplog.records)
    assert any(str(upstream_job.job_id) in r.message for r in caplog.records)


@pytest.mark.integration
async def test_chain_watcher_does_not_reprocess_stamped_event(session_factory):
    """Event already stamped with chain_watcher key → skipped on second poll."""
    *_, event = await _insert_trigger_chain(session_factory)

    count1 = await poll_once(session_factory)
    assert count1 == 1

    count2 = await poll_once(session_factory)
    assert count2 == 0


@pytest.mark.integration
async def test_chain_watcher_ignores_jobs_without_downstream(session_factory):
    """Terminal event for a job with no downstream trigger → ignored."""
    scheduled = datetime.now(tz=UTC) - timedelta(hours=1)
    async with session_factory() as session:
        async with session.begin():
            job = Job(
                user_id="chain-watcher-test",
                description="standalone job",
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
async def test_chain_watcher_two_cursors_independent(session_factory):
    """recurring_watcher and chain_watcher use separate processed_by keys."""
    *_, event = await _insert_trigger_chain(session_factory)

    # Simulate recurring_watcher having already stamped its own key
    async with session_factory() as session:
        async with session.begin():
            ev = (
                await session.execute(select(RunEvent).where(RunEvent.event_id == event.event_id))
            ).scalar_one()
            new_pb = dict(ev.processed_by)
            new_pb["recurring_watcher"] = datetime.now(tz=UTC).isoformat()
            from sqlalchemy import update

            await session.execute(
                update(RunEvent)
                .where(RunEvent.event_id == event.event_id)
                .values(processed_by=new_pb)
            )

    # chain_watcher should still process this event (its own key absent)
    count = await poll_once(session_factory)
    assert count == 1

    async with session_factory() as session:
        async with session.begin():
            refreshed = (
                await session.execute(select(RunEvent).where(RunEvent.event_id == event.event_id))
            ).scalar_one()

    assert "recurring_watcher" in refreshed.processed_by
    assert PROCESSED_BY_KEY in refreshed.processed_by
