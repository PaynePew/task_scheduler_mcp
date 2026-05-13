"""Integration tests for app/workers/reconciler.py — requires running Postgres + ElasticMQ.

Run with:
    uv run pytest -m integration tests/integration/test_reconciler.py
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.engine import create_async_engine
from app.db.models import Job, JobRun, RunEvent
from app.queue.sqs import SQSClient
from app.workers.reconciler import reconcile_once

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

TINY_GRACE = timedelta(seconds=1)  # very small so test rows always exceed it


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


@pytest.fixture
def sqs() -> SQSClient:
    """SQSClient pointed at ElasticMQ; drains leftover messages before each test."""
    client = SQSClient()
    while True:
        msgs = client.receive_messages(max_messages=10, wait_seconds=0)
        if not msgs:
            break
        for msg in msgs:
            client.delete_message(msg["ReceiptHandle"])
    return client


async def _insert_job(factory: async_sessionmaker) -> Job:
    """Insert a bare Job row and return it (committed)."""
    scheduled = datetime.now(tz=UTC) - timedelta(seconds=30)
    async with factory() as session:
        async with session.begin():
            job = Job(
                user_id="reconciler-test",
                description="reconciler test job",
                action="echo",
                action_params={"message": "test"},
                job_type="one_shot",
                scheduled_at=scheduled,
            )
            session.add(job)
            await session.flush()
            job_id = job.job_id
    # Re-fetch outside the closed transaction
    async with factory() as session:
        return (await session.execute(select(Job).where(Job.job_id == job_id))).scalar_one()


async def _insert_run(
    factory: async_sessionmaker,
    job: Job,
    *,
    status: str,
    updated_at: datetime,
) -> JobRun:
    """Insert a JobRun with explicit status and updated_at (committed)."""
    now_bucket = datetime.now(tz=UTC).replace(minute=0, second=0, microsecond=0)
    async with factory() as session:
        async with session.begin():
            run = JobRun(
                time_bucket=now_bucket.isoformat(),
                job_id=job.job_id,
                scheduled_at=job.scheduled_at,
                status=status,
            )
            session.add(run)
            await session.flush()
            run_id = run.run_id

        # Update updated_at via raw SQL to bypass server_default / ORM auto-now logic
        async with session.begin():
            await session.execute(
                update(JobRun).where(JobRun.run_id == run_id).values(updated_at=updated_at)
            )

    async with factory() as session:
        return (await session.execute(select(JobRun).where(JobRun.run_id == run_id))).scalar_one()


# ---------------------------------------------------------------------------
# Test A — DLQ-reconcile: RETRYING older than grace → FAILED
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_reconcile_a_retrying_past_grace_flipped_to_failed(session_factory, sqs):
    """RETRYING row older than DLQ grace → flipped to FAILED + RunEvent with dlq_reconcile."""
    job = await _insert_job(session_factory)
    old_ts = datetime.now(tz=UTC) - timedelta(seconds=600)
    run = await _insert_run(session_factory, job, status="RETRYING", updated_at=old_ts)

    a, b = await reconcile_once(session_factory, sqs, dlq_grace=TINY_GRACE, queued_grace=TINY_GRACE)

    assert a == 1
    assert b == 0

    async with session_factory() as session:
        async with session.begin():
            updated = (
                await session.execute(select(JobRun).where(JobRun.run_id == run.run_id))
            ).scalar_one()
            events = (
                (
                    await session.execute(
                        select(RunEvent).where(
                            RunEvent.run_id == run.run_id, RunEvent.event_type == "FAILED"
                        )
                    )
                )
                .scalars()
                .all()
            )

    assert updated.status == "FAILED"
    assert updated.finish_at is not None
    assert updated.error_message == "exceeded_max_receive (likely DLQ)"

    assert len(events) == 1
    evt = events[0]
    assert evt.status_from == "RETRYING"
    assert evt.status_to == "FAILED"
    assert evt.event_data is not None
    assert evt.event_data["reason"] == "dlq_reconcile"
    # last_seen_at must be the pre-UPDATE stale timestamp, not the reconcile
    # time. Guards against the SQLAlchemy synchronize_session="auto" anti-pattern
    # where reading run.updated_at after a bulk UPDATE returns the new value.
    last_seen = datetime.fromisoformat(evt.event_data["last_seen_at"])
    assert last_seen < datetime.now(tz=UTC) - timedelta(seconds=60)


# ---------------------------------------------------------------------------
# Test B — QUEUED-reconcile: stuck QUEUED → SQS message + RunEvent(REENQUEUED)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_reconcile_b_queued_past_grace_reenqueued(session_factory, sqs):
    """QUEUED row older than grace → SQS message sent + RunEvent(REENQUEUED)."""
    job = await _insert_job(session_factory)
    old_ts = datetime.now(tz=UTC) - timedelta(seconds=600)
    run = await _insert_run(session_factory, job, status="QUEUED", updated_at=old_ts)

    a, b = await reconcile_once(session_factory, sqs, dlq_grace=TINY_GRACE, queued_grace=TINY_GRACE)

    assert a == 0
    assert b == 1

    # SQS must have the message
    msgs = sqs.receive_messages(max_messages=10, wait_seconds=0)
    assert len(msgs) == 1
    body = json.loads(msgs[0]["Body"])
    assert body["run_id"] == run.run_id
    assert body["job_id"] == job.job_id

    async with session_factory() as session:
        async with session.begin():
            updated = (
                await session.execute(select(JobRun).where(JobRun.run_id == run.run_id))
            ).scalar_one()
            events = (
                (
                    await session.execute(
                        select(RunEvent).where(
                            RunEvent.run_id == run.run_id, RunEvent.event_type == "REENQUEUED"
                        )
                    )
                )
                .scalars()
                .all()
            )

    # Status must still be QUEUED (only updated_at advances)
    assert updated.status == "QUEUED"
    # updated_at must have advanced past the old stale value
    assert updated.updated_at > old_ts

    assert len(events) == 1
    evt = events[0]
    assert evt.status_from == "QUEUED"
    assert evt.status_to == "QUEUED"
    assert evt.event_data is not None
    assert evt.event_data["reason"] == "watcher_send_failed"


# ---------------------------------------------------------------------------
# Test C — no false positives: fresh rows inside the grace window are not touched
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_reconcile_c_no_false_positives(session_factory, sqs):
    """Rows inside the grace window are not touched by either sweep."""
    job = await _insert_job(session_factory)

    # Both rows have updated_at = now → inside any grace window > 0
    recent_ts = datetime.now(tz=UTC)
    retrying_run = await _insert_run(session_factory, job, status="RETRYING", updated_at=recent_ts)
    queued_run = await _insert_run(session_factory, job, status="QUEUED", updated_at=recent_ts)

    # Use a large grace window so "recent_ts" is definitely inside it
    big_grace = timedelta(hours=1)
    a, b = await reconcile_once(session_factory, sqs, dlq_grace=big_grace, queued_grace=big_grace)

    assert a == 0, "no RETRYING row should have been touched"
    assert b == 0, "no QUEUED row should have been touched"

    async with session_factory() as session:
        async with session.begin():
            r_run = (
                await session.execute(select(JobRun).where(JobRun.run_id == retrying_run.run_id))
            ).scalar_one()
            q_run = (
                await session.execute(select(JobRun).where(JobRun.run_id == queued_run.run_id))
            ).scalar_one()

    assert r_run.status == "RETRYING", "fresh RETRYING row must remain untouched"
    assert q_run.status == "QUEUED", "fresh QUEUED row must remain untouched"

    # SQS queue must remain empty
    msgs = sqs.receive_messages(max_messages=10, wait_seconds=0)
    assert msgs == [], "no SQS messages should have been sent for in-grace rows"
