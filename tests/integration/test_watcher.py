"""Integration tests for the Watcher — requires running Postgres + ElasticMQ.

Run with:
    docker compose up -d postgres elasticmq && alembic upgrade head
    uv run pytest -m integration tests/integration/test_watcher.py
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.engine import create_async_engine
from app.db.models import Job, JobRun, RunEvent
from app.queue.sqs import SQSClient
from app.workers.watcher import claim_and_publish

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def session_factory():
    """Fresh engine per module-level test session; cleans all job data on teardown."""
    engine = create_async_engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    async with factory() as session:
        async with session.begin():
            await session.execute(text("DELETE FROM run_events"))
            await session.execute(text("DELETE FROM job_runs"))
            await session.execute(text("DELETE FROM jobs"))
    await engine.dispose()


@pytest.fixture
def sqs() -> SQSClient:
    """SQSClient pointed at ElasticMQ; drains the queue before each test."""
    client = SQSClient()
    # Drain any leftover messages from a previous run
    while True:
        msgs = client.receive_messages(max_messages=10, wait_seconds=0)
        if not msgs:
            break
        for msg in msgs:
            client.delete_message(msg["ReceiptHandle"])
    return client


async def _insert_pending_run(
    factory: async_sessionmaker,
    *,
    scheduled_at: datetime,
    status: str = "PENDING",
) -> tuple[Job, JobRun]:
    """Helper: insert a Job + JobRun with the given scheduled_at and status, committed."""
    async with factory() as session:
        async with session.begin():
            job = Job(
                user_id="watcher-test",
                description="echo",
                action="echo",
                action_params={"message": "hi"},
                job_type="one_shot",
                scheduled_at=scheduled_at,
            )
            session.add(job)
            await session.flush()

            now_bucket = datetime.now(tz=UTC).replace(minute=0, second=0, microsecond=0)
            run = JobRun(
                time_bucket=now_bucket.isoformat(),
                job_id=job.job_id,
                scheduled_at=scheduled_at,
                status=status,
            )
            session.add(run)
    return job, run


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_watcher_picks_up_pending_run(session_factory, sqs):
    """PENDING run due in the past → claimed → QUEUED + RunEvent + SQS message."""
    past = datetime.now(tz=UTC) - timedelta(seconds=60)
    _, run = await _insert_pending_run(session_factory, scheduled_at=past)

    count = await claim_and_publish(session_factory, sqs)
    assert count == 1

    # DB assertions
    from sqlalchemy.future import select

    async with session_factory() as session:
        async with session.begin():
            updated_run = (
                await session.execute(select(JobRun).where(JobRun.run_id == run.run_id))
            ).scalar_one()
            events = (
                (
                    await session.execute(
                        select(RunEvent).where(
                            RunEvent.run_id == run.run_id, RunEvent.event_type == "QUEUED"
                        )
                    )
                )
                .scalars()
                .all()
            )

    assert updated_run.status == "QUEUED"
    assert len(events) == 1
    assert events[0].status_from == "PENDING"
    assert events[0].status_to == "QUEUED"

    # SQS assertion — message should be immediately visible (DelaySeconds=0 for past runs)
    msgs = sqs.receive_messages(max_messages=10, wait_seconds=0)
    assert len(msgs) == 1
    body = json.loads(msgs[0]["Body"])
    assert body["run_id"] == run.run_id


@pytest.mark.integration
async def test_watcher_ignores_cancelled_runs(session_factory, sqs):
    """CANCELLED runs must not be picked up, even if scheduled_at is past."""
    past = datetime.now(tz=UTC) - timedelta(seconds=60)
    _, run = await _insert_pending_run(session_factory, scheduled_at=past, status="CANCELLED")

    count = await claim_and_publish(session_factory, sqs)
    assert count == 0

    # Run should remain CANCELLED
    from sqlalchemy.future import select

    async with session_factory() as session:
        async with session.begin():
            unchanged = (
                await session.execute(select(JobRun).where(JobRun.run_id == run.run_id))
            ).scalar_one()
    assert unchanged.status == "CANCELLED"

    # Queue must be empty
    assert sqs.receive_messages(max_messages=1, wait_seconds=0) == []


@pytest.mark.integration
async def test_watcher_sqs_message_delay_for_future_run(session_factory, sqs):
    """Run scheduled 2 min out → DelaySeconds ≈ 120 (within lookahead window)."""
    future = datetime.now(tz=UTC) + timedelta(seconds=120)
    _, run = await _insert_pending_run(session_factory, scheduled_at=future)

    count = await claim_and_publish(session_factory, sqs)
    assert count == 1

    # Message is NOT immediately visible (DelaySeconds > 0), so receive returns nothing
    msgs = sqs.receive_messages(max_messages=1, wait_seconds=0)
    assert msgs == [], "message should still be delayed"


@pytest.mark.integration
async def test_two_concurrent_watchers_do_not_double_publish(session_factory, sqs):
    """FOR UPDATE SKIP LOCKED: two concurrent watchers claim the same run exactly once."""
    past = datetime.now(tz=UTC) - timedelta(seconds=30)
    _, run = await _insert_pending_run(session_factory, scheduled_at=past)

    # Run two watcher ticks concurrently
    counts = await asyncio.gather(
        claim_and_publish(session_factory, sqs),
        claim_and_publish(session_factory, sqs),
    )

    # Exactly one watcher wins; total claimed == 1
    assert sum(counts) == 1

    # Exactly one SQS message
    msgs = sqs.receive_messages(max_messages=10, wait_seconds=0)
    assert len(msgs) == 1
    body = json.loads(msgs[0]["Body"])
    assert body["run_id"] == run.run_id

    # Exactly one RunEvent(QUEUED) in DB
    from sqlalchemy.future import select

    async with session_factory() as session:
        async with session.begin():
            events = (
                (
                    await session.execute(
                        select(RunEvent).where(
                            RunEvent.run_id == run.run_id, RunEvent.event_type == "QUEUED"
                        )
                    )
                )
                .scalars()
                .all()
            )
    assert len(events) == 1
