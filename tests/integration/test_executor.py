"""Integration tests for app/workers/executor.py — requires running Postgres + ElasticMQ.

Run with:
    docker compose up -d postgres elasticmq && alembic upgrade head
    uv run pytest -m integration tests/integration/test_executor.py
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.actions.base import ActionResult
from app.actions.registry import ACTION_REGISTRY
from app.db.engine import create_async_engine
from app.db.models import Job, JobRun, RunEvent
from app.queue.sqs import SQSClient
from app.workers.executor import process_one

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


async def _insert_queued_run(
    factory: async_sessionmaker,
    *,
    action: str = "echo",
    action_params: dict | None = None,
    status: str = "QUEUED",
) -> tuple[Job, JobRun]:
    """Insert a Job + JobRun at the given status, committed."""
    if action_params is None:
        action_params = {"message": "hello from integration test"}
    scheduled = datetime.now(tz=UTC) - timedelta(seconds=10)
    async with factory() as session:
        async with session.begin():
            job = Job(
                user_id="executor-test",
                description="test job",
                action=action,
                action_params=action_params,
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
                status=status,
            )
            session.add(run)
    return job, run


def _make_sqs_message(run_id: int, job_id: int) -> dict:
    return {
        "Body": json.dumps({"run_id": run_id, "job_id": job_id}),
        "ReceiptHandle": f"fake-receipt-{run_id}",
        "MessageId": f"fake-msg-{run_id}",
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_queued_echo_run_completes_to_succeeded(session_factory, sqs):
    """QUEUED echo run → process_one → SUCCEEDED + RunEvent(SUCCEEDED) + DeleteMessage."""
    job, run = await _insert_queued_run(session_factory)
    message = _make_sqs_message(run.run_id, job.job_id)

    deleted_receipts: list[str] = []
    orig_delete = sqs.delete_message

    def _capture_delete(receipt):
        deleted_receipts.append(receipt)
        orig_delete(receipt)

    sqs.delete_message = _capture_delete

    await process_one(session_factory, sqs, message, registry=ACTION_REGISTRY)

    # DB: run should be SUCCEEDED
    async with session_factory() as session:
        async with session.begin():
            updated_run = (
                await session.execute(select(JobRun).where(JobRun.run_id == run.run_id))
            ).scalar_one()
            events = (
                (await session.execute(select(RunEvent).where(RunEvent.run_id == run.run_id)))
                .scalars()
                .all()
            )

    assert updated_run.status == "SUCCEEDED"
    assert updated_run.start_at is not None
    assert updated_run.finish_at is not None

    event_types = {e.event_type for e in events}
    assert "STARTED" in event_types
    assert "SUCCEEDED" in event_types

    started = next(e for e in events if e.event_type == "STARTED")
    assert started.status_from == "QUEUED"
    assert started.status_to == "RUNNING"

    succeeded = next(e for e in events if e.event_type == "SUCCEEDED")
    assert succeeded.status_from == "RUNNING"
    assert succeeded.status_to == "SUCCEEDED"

    # SQS message was deleted
    assert message["ReceiptHandle"] in deleted_receipts


@pytest.mark.integration
async def test_handler_permanent_failure_marks_run_failed(session_factory, sqs):
    """Handler returning ok=False, retryable=False → FAILED + RunEvent(FAILED) + DeleteMessage."""

    class NoParams(BaseModel):
        pass

    class AlwaysFailHandler:
        name = "always_fail"
        params_model = NoParams
        timeout_seconds = 10

        async def execute(self, run, params) -> ActionResult:
            return ActionResult(ok=False, result=None, error="permanent error", retryable=False)

    job, run = await _insert_queued_run(session_factory, action="always_fail", action_params={})
    message = _make_sqs_message(run.run_id, job.job_id)

    registry = {"always_fail": AlwaysFailHandler()}

    deleted_receipts: list[str] = []
    orig_delete = sqs.delete_message

    def _capture_delete(receipt):
        deleted_receipts.append(receipt)
        orig_delete(receipt)

    sqs.delete_message = _capture_delete

    await process_one(session_factory, sqs, message, registry=registry)

    async with session_factory() as session:
        async with session.begin():
            updated_run = (
                await session.execute(select(JobRun).where(JobRun.run_id == run.run_id))
            ).scalar_one()
            events = (
                (await session.execute(select(RunEvent).where(RunEvent.run_id == run.run_id)))
                .scalars()
                .all()
            )

    assert updated_run.status == "FAILED"
    assert updated_run.error_message == "permanent error"

    event_types = {e.event_type for e in events}
    assert "STARTED" in event_types
    assert "FAILED" in event_types

    assert message["ReceiptHandle"] in deleted_receipts


@pytest.mark.integration
async def test_concurrent_workers_duplicate_message_exactly_one_execution(session_factory, sqs):
    """Two workers receiving the same redelivered message: exactly one executes."""
    job, run = await _insert_queued_run(session_factory)
    message = _make_sqs_message(run.run_id, job.job_id)

    delete_count = 0
    orig_delete = sqs.delete_message

    def _count_delete(receipt):
        nonlocal delete_count
        delete_count += 1
        # Simulate successful delete (ignore "not found" for duplicate receipts)
        try:
            orig_delete(receipt)
        except Exception:
            pass

    sqs.delete_message = _count_delete

    # Run two workers concurrently with the same message (simulating duplicate delivery)
    await asyncio.gather(
        process_one(session_factory, sqs, message, registry=ACTION_REGISTRY),
        process_one(session_factory, sqs, message, registry=ACTION_REGISTRY),
    )

    # DB: exactly one SUCCEEDED event
    async with session_factory() as session:
        async with session.begin():
            updated_run = (
                await session.execute(select(JobRun).where(JobRun.run_id == run.run_id))
            ).scalar_one()
            succeeded_events = (
                (
                    await session.execute(
                        select(RunEvent).where(
                            RunEvent.run_id == run.run_id,
                            RunEvent.event_type == "SUCCEEDED",
                        )
                    )
                )
                .scalars()
                .all()
            )
            started_events = (
                (
                    await session.execute(
                        select(RunEvent).where(
                            RunEvent.run_id == run.run_id,
                            RunEvent.event_type == "STARTED",
                        )
                    )
                )
                .scalars()
                .all()
            )

    assert updated_run.status == "SUCCEEDED"
    assert len(succeeded_events) == 1, "exactly one SUCCEEDED event"
    assert len(started_events) == 1, "exactly one STARTED event"

    # Both workers call delete_message: winner after completion, loser after no-op claim
    assert delete_count == 2
