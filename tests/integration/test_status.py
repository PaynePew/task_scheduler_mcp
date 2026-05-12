"""Integration tests for task.status@v1 — requires running Postgres + ElasticMQ.

Run with:
    docker compose up -d postgres elasticmq && alembic upgrade head
    uv run pytest -m integration tests/integration/test_status.py
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.engine import create_async_engine
from app.db.models import Job
from app.domain.jobs import create_job
from app.mcp.handlers.status import handle_task_status
from app.queue.sqs import SQSClient
from app.workers.executor import process_one

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


async def _create_immediate_echo(
    factory: async_sessionmaker[AsyncSession],
    *,
    user_id: str = "status-test-user",
) -> Job:
    """Create an immediate echo job via the domain layer."""
    async with factory() as session:
        return await create_job(
            session,
            user_id=user_id,
            action="echo",
            action_params={"message": "hello"},
            schedule_type="immediate",
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_status_returns_scheduled_for_new_job(session_factory, sqs):
    """Newly created immediate echo job → external status is 'scheduled'."""
    job = await _create_immediate_echo(session_factory)

    result = await handle_task_status(
        {"job_id": job.job_id},
        user_id="status-test-user",
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "scheduled"
    assert result["data"]["job_id"] == job.job_id
    assert result["data"]["action"] == "echo"


@pytest.mark.integration
async def test_status_returns_completed_after_worker_executes(session_factory, sqs):
    """After worker processes the run, status transitions to 'completed'."""
    job = await _create_immediate_echo(session_factory)

    # Watcher: claim PENDING → QUEUED and enqueue to SQS.
    from app.workers.watcher import claim_and_publish

    await claim_and_publish(session_factory, sqs)

    # Worker: receive and process the SQS message.
    msgs = sqs.receive_messages(max_messages=1, wait_seconds=0)
    assert len(msgs) == 1, "watcher should have enqueued one message"

    await process_one(session_factory, sqs, msgs[0])

    # Status should now be 'completed'.
    result = await handle_task_status(
        {"job_id": job.job_id},
        user_id="status-test-user",
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "completed"


@pytest.mark.integration
async def test_status_returns_completed_with_runs(session_factory, sqs):
    """include_runs=true returns the run history after worker completion."""
    job = await _create_immediate_echo(session_factory)

    from app.workers.watcher import claim_and_publish

    await claim_and_publish(session_factory, sqs)
    msgs = sqs.receive_messages(max_messages=1, wait_seconds=0)
    await process_one(session_factory, sqs, msgs[0])

    result = await handle_task_status(
        {"job_id": job.job_id, "include_runs": True},
        user_id="status-test-user",
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "completed"
    runs = result["data"]["runs"]
    assert len(runs) == 1
    assert runs[0]["status"] == "completed"
    assert runs[0]["run_id"] is not None
    assert runs[0]["scheduled_at"] is not None


@pytest.mark.integration
async def test_status_not_found_for_nonexistent_job(session_factory, sqs):
    """Nonexistent job_id → NOT_FOUND error."""
    result = await handle_task_status(
        {"job_id": 999999999},
        user_id="status-test-user",
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "NOT_FOUND"


@pytest.mark.integration
async def test_status_not_found_for_cross_user_job(session_factory, sqs):
    """Cross-user job_id returns NOT_FOUND (no information leak)."""
    job = await _create_immediate_echo(session_factory, user_id="owner-user")

    result = await handle_task_status(
        {"job_id": job.job_id},
        user_id="attacker-user",
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "NOT_FOUND"
