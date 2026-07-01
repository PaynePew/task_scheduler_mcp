"""Integration tests for task.status.v1 — requires running Postgres + ElasticMQ.

Run with:
    docker compose up -d postgres elasticmq && alembic upgrade head
    uv run pytest -m integration tests/integration/test_status.py
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.engine import create_async_engine
from app.db.models import Job, JobRun, RunEvent
from app.domain.jobs import cancel_job, create_job
from app.mcp.handlers.status import handle_task_status
from app.queue.sqs import SQSClient
from app.workers.executor import process_one
from app.workers.recurring_watcher import poll_once as continuation_poll_once

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


async def _seed_job(
    factory: async_sessionmaker[AsyncSession],
    *,
    user_id: str,
    trigger_on_job_id: int | None = None,
    trigger_on_status: str | None = None,
    state: str = "active",
) -> int:
    """Insert one immediate/one-shot Job directly; return its job_id.

    Bypasses create_job's V3 guard (a chain's trigger must have a live run) so a
    chained downstream can be seeded before its trigger has ever fired — the same
    pattern tests/integration/test_chain_settle.py uses.
    """
    now = datetime.now(tz=UTC)
    async with factory() as session:
        async with session.begin():
            job = Job(
                user_id=user_id,
                description="status-chain-test",
                action="echo",
                action_params={},
                job_type="one_shot",
                scheduled_at=now,
                timezone="UTC",
                trigger_on_job_id=trigger_on_job_id,
                trigger_on_status=trigger_on_status,
                state=state,
            )
            session.add(job)
            await session.flush()
            return job.job_id


async def _seed_terminal_run_with_event(
    factory: async_sessionmaker[AsyncSession],
    *,
    job_id: int,
    user_id: str,
    status: str = "SUCCEEDED",
) -> None:
    """Insert one terminal JobRun + its RunEvent for the continuation consumer."""
    now = datetime.now(tz=UTC)
    bucket = now.replace(minute=0, second=0, microsecond=0).isoformat()
    async with factory() as session:
        async with session.begin():
            run = JobRun(
                time_bucket=bucket,
                job_id=job_id,
                user_id=user_id,
                scheduled_at=now,
                status=status,
                finish_at=now,
            )
            session.add(run)
            await session.flush()
            session.add(
                RunEvent(
                    run_id=run.run_id,
                    job_id=job_id,
                    event_type=status,
                    status_from="RUNNING",
                    status_to=status,
                    occurred_at=now,
                )
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
        session_factory=session_factory,
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
        session_factory=session_factory,
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
        session_factory=session_factory,
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
        session_factory=session_factory,
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
        session_factory=session_factory,
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# External status derivation from (Job.state, latest run) — ADR-067 §9, #256.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_status_not_yet_triggered_downstream_is_scheduled_with_empty_runs(
    session_factory, sqs
):
    """A chained downstream with no run yet (still waiting on its trigger) shows
    as 'scheduled' with an empty runs list — Job.state, not a fake 'PENDING'."""
    user = "chain-status-user"
    a = await _seed_job(session_factory, user_id=user)
    b = await _seed_job(session_factory, user_id=user, trigger_on_job_id=a, trigger_on_status="ANY")

    result = await handle_task_status(
        {"job_id": b, "include_runs": True}, user_id=user, session_factory=session_factory
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "scheduled"
    assert result["data"]["runs"] == []


@pytest.mark.integration
async def test_status_surfaces_triggered_by_for_chained_job(session_factory, sqs):
    """task.status surfaces triggered_by:<job_id> for a trigger-driven job."""
    user = "chain-status-user2"
    a = await _seed_job(session_factory, user_id=user)
    b = await _seed_job(session_factory, user_id=user, trigger_on_job_id=a, trigger_on_status="ANY")

    result = await handle_task_status({"job_id": b}, user_id=user, session_factory=session_factory)

    assert result["ok"] is True
    assert result["data"]["triggered_by"] == a


@pytest.mark.integration
async def test_status_omits_triggered_by_for_non_chained_job(session_factory, sqs):
    """A schedule-driven job (no trigger) never carries a triggered_by field."""
    job = await _create_immediate_echo(session_factory, user_id="no-chain-user")

    result = await handle_task_status(
        {"job_id": job.job_id}, user_id="no-chain-user", session_factory=session_factory
    )

    assert result["ok"] is True
    assert "triggered_by" not in result["data"]


@pytest.mark.integration
async def test_status_predicate_miss_settled_downstream_is_completed(session_factory, sqs):
    """A chained downstream that never fired (predicate miss) but has settled to
    Job.state='completed' reports 'completed', not the no-run default 'scheduled'."""
    user = "predicate-miss-user"
    a = await _seed_job(session_factory, user_id=user, state="completed")
    await _seed_terminal_run_with_event(session_factory, job_id=a, user_id=user)
    b = await _seed_job(
        session_factory, user_id=user, trigger_on_job_id=a, trigger_on_status="FAILED"
    )

    # A succeeded but B triggers on FAILED — predicate miss: B never gets a run,
    # and the continuation consumer settles it since the parent is terminal.
    await continuation_poll_once(session_factory)

    result = await handle_task_status(
        {"job_id": b, "include_runs": True}, user_id=user, session_factory=session_factory
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "completed"
    assert result["data"]["runs"] == []


@pytest.mark.integration
async def test_status_cancelled_before_fired_downstream_is_cancelled(session_factory, sqs):
    """Cancelling a not-yet-fired chained downstream directly reports 'cancelled',
    not the no-run default 'scheduled'."""
    user = "cancel-chain-user"
    a = await _seed_job(session_factory, user_id=user)
    b = await _seed_job(session_factory, user_id=user, trigger_on_job_id=a, trigger_on_status="ANY")

    async with session_factory() as session:
        await cancel_job(session, user_id=user, job_id=b)

    result = await handle_task_status({"job_id": b}, user_id=user, session_factory=session_factory)

    assert result["ok"] is True
    assert result["data"]["status"] == "cancelled"
