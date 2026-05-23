"""Integration tests for recurring-chain from_run_id auto-wiring (issue #202).

Verifies that executor auto-wires params.from_run_id = jobrun.wait_for_run_id
for recurring chains where the caller left from_run_id unset at job create time.

Run with:
    uv run pytest -m integration tests/integration/test_chain_recurring.py
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.actions.base import ActionResult
from app.db.engine import create_async_engine
from app.db.models import Job, JobRun
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
    """SQSClient with delete_message stubbed out (messages are synthetic)."""
    client = SQSClient()
    client.delete_message = lambda receipt: None
    return client


def _sqs_message(run_id: int, job_id: int) -> dict:
    return {
        "Body": json.dumps({"run_id": run_id, "job_id": job_id}),
        "ReceiptHandle": f"fake-receipt-{run_id}",
        "MessageId": f"fake-msg-{run_id}",
    }


# ---------------------------------------------------------------------------
# Test handler: captures params.from_run_id at execute() time
# ---------------------------------------------------------------------------


class _CaptureParams(BaseModel):
    from_run_id: int | None = None


class _CapturingHandler:
    """Minimal handler that records the from_run_id it received and stores it in result."""

    name = "capturing_chain"
    params_model = _CaptureParams
    timeout_seconds = 10

    async def execute(self, run: JobRun, params: _CaptureParams) -> ActionResult:
        return ActionResult(
            ok=True,
            result={"received_from_run_id": params.from_run_id},
            error=None,
        )


_REGISTRY = {"capturing_chain": _CapturingHandler()}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _insert_upstream_run(
    factory: async_sessionmaker,
    *,
    job: Job,
    result: dict,
    scheduled_offset_seconds: int = 0,
) -> JobRun:
    """Insert an already-SUCCEEDED upstream JobRun with a given result."""
    base = datetime.now(tz=UTC) - timedelta(hours=1)
    scheduled = base + timedelta(seconds=scheduled_offset_seconds)
    bucket = scheduled.replace(minute=0, second=0, microsecond=0).isoformat()
    async with factory() as session:
        async with session.begin():
            run = JobRun(
                time_bucket=bucket,
                job_id=job.job_id,
                user_id=job.user_id,
                scheduled_at=scheduled,
                status="SUCCEEDED",
                finish_at=datetime.now(tz=UTC),
                result=json.dumps(result),
            )
            session.add(run)
    return run


async def _insert_downstream_run(
    factory: async_sessionmaker,
    *,
    job: Job,
    wait_for_run_id: int,
    scheduled_offset_seconds: int = 0,
) -> JobRun:
    """Insert a PENDING downstream JobRun that waits for wait_for_run_id.

    Simulates what ChainWatcher produces after flipping WAITING → PENDING.
    """
    base = datetime.now(tz=UTC) - timedelta(hours=1)
    scheduled = base + timedelta(seconds=scheduled_offset_seconds)
    bucket = scheduled.replace(minute=0, second=0, microsecond=0).isoformat()
    async with factory() as session:
        async with session.begin():
            run = JobRun(
                time_bucket=bucket,
                job_id=job.job_id,
                user_id=job.user_id,
                scheduled_at=scheduled,
                status="PENDING",
                wait_for_run_id=wait_for_run_id,
            )
            session.add(run)
    return run


# ---------------------------------------------------------------------------
# Core test: 3 ticks, each B run sees its own A tick's run_id
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_recurring_chain_auto_wires_from_run_id_across_3_ticks(session_factory, sqs):
    """Recurring chain: each downstream run receives the correct upstream run_id.

    Job A: recurring upstream (produces structured result per tick)
    Job B: recurring downstream, trigger_on_job_id=A, from_run_id NOT set at create time

    After ChainWatcher flips each B run to PENDING (with wait_for_run_id=A_tick_N.run_id),
    executor must inject params.from_run_id = jobrun.wait_for_run_id so each B tick
    reads its own A tick — not a stale first-tick run_id.
    """
    # -----------------------------------------------------------------------
    # Setup: two recurring jobs
    # -----------------------------------------------------------------------
    async with session_factory() as session:
        async with session.begin():
            job_a = Job(
                user_id="chain-recurring-test",
                description="upstream recurring",
                action="capturing_chain",
                action_params={},
                job_type="recurring",
                cron_expr="@hourly",
                timezone="UTC",
            )
            session.add(job_a)
            await session.flush()

            # B has from_run_id absent (None) in action_params — the bug case
            job_b = Job(
                user_id="chain-recurring-test",
                description="downstream recurring chain",
                action="capturing_chain",
                action_params={},  # from_run_id intentionally absent
                job_type="recurring",
                cron_expr="@hourly",
                timezone="UTC",
                trigger_on_job_id=job_a.job_id,
                trigger_on_status="ANY",
            )
            session.add(job_b)

    # -----------------------------------------------------------------------
    # Tick 1: A produces result_1; B run waits for A's tick-1 run_id
    # -----------------------------------------------------------------------
    run_a1 = await _insert_upstream_run(
        session_factory,
        job=job_a,
        result={"tick": 1, "issues_closed": 3},
        scheduled_offset_seconds=0,
    )
    run_b1 = await _insert_downstream_run(
        session_factory,
        job=job_b,
        wait_for_run_id=run_a1.run_id,
        scheduled_offset_seconds=1,
    )

    # -----------------------------------------------------------------------
    # Tick 2: different bucket (1 hour later offset)
    # -----------------------------------------------------------------------
    run_a2 = await _insert_upstream_run(
        session_factory,
        job=job_a,
        result={"tick": 2, "issues_closed": 7},
        scheduled_offset_seconds=3600,
    )
    run_b2 = await _insert_downstream_run(
        session_factory,
        job=job_b,
        wait_for_run_id=run_a2.run_id,
        scheduled_offset_seconds=3601,
    )

    # -----------------------------------------------------------------------
    # Tick 3
    # -----------------------------------------------------------------------
    run_a3 = await _insert_upstream_run(
        session_factory,
        job=job_a,
        result={"tick": 3, "issues_closed": 12},
        scheduled_offset_seconds=7200,
    )
    run_b3 = await _insert_downstream_run(
        session_factory,
        job=job_b,
        wait_for_run_id=run_a3.run_id,
        scheduled_offset_seconds=7201,
    )

    # -----------------------------------------------------------------------
    # Execute all three downstream runs via process_one
    # -----------------------------------------------------------------------
    for run_b in (run_b1, run_b2, run_b3):
        msg = _sqs_message(run_b.run_id, job_b.job_id)
        await process_one(session_factory, sqs, msg, registry=_REGISTRY)

    # -----------------------------------------------------------------------
    # Assert: each B run recorded the correct upstream run_id (not a stale one)
    # -----------------------------------------------------------------------
    async with session_factory() as session:
        async with session.begin():
            b_runs = (
                (
                    await session.execute(
                        select(JobRun).where(
                            JobRun.job_id == job_b.job_id,
                            JobRun.status == "SUCCEEDED",
                        )
                    )
                )
                .scalars()
                .all()
            )

    assert len(b_runs) == 3, f"expected 3 SUCCEEDED B runs, got {len(b_runs)}"

    by_run_id = {r.run_id: json.loads(r.result) for r in b_runs}

    assert by_run_id[run_b1.run_id]["received_from_run_id"] == run_a1.run_id, (
        f"tick 1: expected from_run_id={run_a1.run_id}, "
        f"got {by_run_id[run_b1.run_id]['received_from_run_id']}"
    )
    assert by_run_id[run_b2.run_id]["received_from_run_id"] == run_a2.run_id, (
        f"tick 2: expected from_run_id={run_a2.run_id}, "
        f"got {by_run_id[run_b2.run_id]['received_from_run_id']}"
    )
    assert by_run_id[run_b3.run_id]["received_from_run_id"] == run_a3.run_id, (
        f"tick 3: expected from_run_id={run_a3.run_id}, "
        f"got {by_run_id[run_b3.run_id]['received_from_run_id']}"
    )


# ---------------------------------------------------------------------------
# One-shot chain regression: explicit from_run_id must NOT be overwritten
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_one_shot_chain_explicit_from_run_id_not_overwritten(session_factory, sqs):
    """One-shot chain: when from_run_id is set at job create time, executor must not override it.

    This ensures the fix only applies when from_run_id is None (unset) — not when
    the caller already hard-coded a specific upstream run_id.
    """
    async with session_factory() as session:
        async with session.begin():
            job_a = Job(
                user_id="one-shot-chain-test",
                description="upstream one-shot",
                action="capturing_chain",
                action_params={},
                job_type="one_shot",
                scheduled_at=datetime.now(tz=UTC) - timedelta(hours=2),
            )
            session.add(job_a)
            await session.flush()

            # Upstream run that the downstream was wired to at create time
            bucket = (
                (datetime.now(tz=UTC) - timedelta(hours=2))
                .replace(minute=0, second=0, microsecond=0)
                .isoformat()
            )
            run_a_original = JobRun(
                time_bucket=bucket,
                job_id=job_a.job_id,
                user_id=job_a.user_id,
                scheduled_at=datetime.now(tz=UTC) - timedelta(hours=2),
                status="SUCCEEDED",
                finish_at=datetime.now(tz=UTC),
                result=json.dumps({"tick": "original"}),
            )
            session.add(run_a_original)
            await session.flush()

            # A later A run — this is what wait_for_run_id would point to if recurring
            run_a_later = JobRun(
                time_bucket=bucket,
                job_id=job_a.job_id,
                user_id=job_a.user_id,
                scheduled_at=datetime.now(tz=UTC) - timedelta(hours=1),
                status="SUCCEEDED",
                finish_at=datetime.now(tz=UTC),
                result=json.dumps({"tick": "later"}),
            )
            session.add(run_a_later)
            await session.flush()

            # B job: from_run_id explicitly set to run_a_original (one-shot wiring)
            job_b = Job(
                user_id="one-shot-chain-test",
                description="downstream one-shot with explicit from_run_id",
                action="capturing_chain",
                action_params={"from_run_id": run_a_original.run_id},
                job_type="one_shot",
                scheduled_at=datetime.now(tz=UTC) - timedelta(minutes=30),
            )
            session.add(job_b)
            await session.flush()

            # B's run — wait_for_run_id points to the LATER A run (as if chain flip occurred)
            # but from_run_id is already set in action_params → should NOT be overwritten
            bucket_b = (
                (datetime.now(tz=UTC) - timedelta(minutes=30))
                .replace(minute=0, second=0, microsecond=0)
                .isoformat()
            )
            run_b = JobRun(
                time_bucket=bucket_b,
                job_id=job_b.job_id,
                user_id=job_b.user_id,
                scheduled_at=datetime.now(tz=UTC) - timedelta(minutes=30),
                status="PENDING",
                wait_for_run_id=run_a_later.run_id,  # different from action_params from_run_id
            )
            session.add(run_b)

    msg = _sqs_message(run_b.run_id, job_b.job_id)
    await process_one(session_factory, sqs, msg, registry=_REGISTRY)

    async with session_factory() as session:
        async with session.begin():
            updated_run = (
                await session.execute(select(JobRun).where(JobRun.run_id == run_b.run_id))
            ).scalar_one()

    assert updated_run.status == "SUCCEEDED"
    result = json.loads(updated_run.result)
    # Must have used the ORIGINAL from_run_id, not the wait_for_run_id
    assert result["received_from_run_id"] == run_a_original.run_id, (
        f"expected original from_run_id={run_a_original.run_id}, "
        f"got {result['received_from_run_id']} (wait_for_run_id={run_a_later.run_id})"
    )


# ---------------------------------------------------------------------------
# Non-chained recurring job: wait_for_run_id=None → no injection
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_non_chained_job_no_from_run_id_injection(session_factory, sqs):
    """Non-chained job (wait_for_run_id=None): no injection; from_run_id stays None."""
    async with session_factory() as session:
        async with session.begin():
            job = Job(
                user_id="non-chained-test",
                description="standalone recurring",
                action="capturing_chain",
                action_params={},  # from_run_id absent
                job_type="recurring",
                cron_expr="@hourly",
                timezone="UTC",
            )
            session.add(job)
            await session.flush()

            bucket = (
                (datetime.now(tz=UTC) - timedelta(hours=1))
                .replace(minute=0, second=0, microsecond=0)
                .isoformat()
            )
            run = JobRun(
                time_bucket=bucket,
                job_id=job.job_id,
                user_id=job.user_id,
                scheduled_at=datetime.now(tz=UTC) - timedelta(hours=1),
                status="PENDING",
                wait_for_run_id=None,  # no upstream dependency
            )
            session.add(run)

    msg = _sqs_message(run.run_id, job.job_id)
    await process_one(session_factory, sqs, msg, registry=_REGISTRY)

    async with session_factory() as session:
        async with session.begin():
            updated_run = (
                await session.execute(select(JobRun).where(JobRun.run_id == run.run_id))
            ).scalar_one()

    assert updated_run.status == "SUCCEEDED"
    result = json.loads(updated_run.result)
    assert result["received_from_run_id"] is None, (
        f"expected no injection (None), got {result['received_from_run_id']}"
    )
