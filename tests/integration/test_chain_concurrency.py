"""Integration tests: continuation correctness under redelivery + multi-tick (issue #254).

Pre-ADR-067 this suite exercised the two-consumer race: ``RecurringJobWatcher`` and
``ChainWatcher`` consuming the same upstream terminal event concurrently, with the
recurring poll arming tick N+1's ``WAITING`` run before the flip of tick N. ADR-067
removes that control plane — there is now **one continuation consumer** that creates
the downstream run when its upstream terminates, in one transaction. The remaining
concurrency concern is a *crashed / duplicated consumer*: a terminal event redelivered
because the consumer died before stamping its cursor must not double-create
(exactly-once is a data-layer guarantee, not an operational hope).

These tests drive the REAL continuation path (no faked downstream insert) and assert:
  1. per-tick re-fire is idempotent under redelivery (no double-create);
  2. a downstream that never drains has its overlapping ticks skipped (not created);
  3. a fan-out cascade re-fires every tick, each run reading ITS OWN upstream run_id.

Run with:
    uv run pytest -m integration tests/integration/test_chain_concurrency.py
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from pydantic import BaseModel
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.actions.base import ActionResult
from app.db.engine import create_async_engine
from app.db.models import Job, JobRun, RunEvent
from app.queue.sqs import SQSClient
from app.workers.executor import process_one
from app.workers.recurring_watcher import poll_once as continuation_poll_once

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
    requires_operator = False

    async def execute(self, run: JobRun, params: _CaptureParams) -> ActionResult:
        return ActionResult(
            ok=True, result={"received_from_run_id": params.from_run_id}, error=None
        )


_REGISTRY = {"capturing_chain": _CapturingHandler()}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _terminate_run(
    factory: async_sessionmaker,
    run: JobRun,
    *,
    status: str = "SUCCEEDED",
    result: dict | None = None,
) -> int:
    """Mark *run* terminal + emit the matching RunEvent. Returns the event_id."""
    now = datetime.now(tz=UTC)
    async with factory() as session:
        async with session.begin():
            values: dict = {"status": status, "finish_at": now}
            if result is not None:
                values["result"] = json.dumps(result)
            await session.execute(
                update(JobRun).where(JobRun.run_id == run.run_id).values(**values)
            )
            event = RunEvent(
                run_id=run.run_id,
                job_id=run.job_id,
                event_type=status,
                status_from="RUNNING",
                status_to=status,
                occurred_at=now,
            )
            session.add(event)
            await session.flush()
            return event.event_id


async def _redeliver(factory: async_sessionmaker, event_id: int) -> None:
    """Clear an event's cursor and re-poll — a consumer that crashed before stamping."""
    async with factory() as session:
        async with session.begin():
            await session.execute(
                update(RunEvent).where(RunEvent.event_id == event_id).values(processed_by={})
            )
    await continuation_poll_once(factory)


async def _runs_by_status(factory: async_sessionmaker, job_id: int, status: str) -> list[JobRun]:
    async with factory() as session:
        async with session.begin():
            return (
                (
                    await session.execute(
                        select(JobRun).where(JobRun.job_id == job_id, JobRun.status == status)
                    )
                )
                .scalars()
                .all()
            )


async def _all_runs(factory: async_sessionmaker, job_id: int) -> list[JobRun]:
    async with factory() as session:
        async with session.begin():
            return (
                (await session.execute(select(JobRun).where(JobRun.job_id == job_id)))
                .scalars()
                .all()
            )


async def _make_recurring_root(factory: async_sessionmaker, *, user_id: str) -> tuple[Job, JobRun]:
    now = datetime.now(tz=UTC)
    bucket = now.replace(minute=0, second=0, microsecond=0).isoformat()
    async with factory() as session:
        async with session.begin():
            job = Job(
                user_id=user_id,
                description="root recurring",
                action="capturing_chain",
                action_params={},
                job_type="recurring",
                cron_expr="@hourly",
                timezone="UTC",
            )
            session.add(job)
            await session.flush()
            run0 = JobRun(
                time_bucket=bucket,
                job_id=job.job_id,
                user_id=job.user_id,
                scheduled_at=now,
                status="PENDING",
            )
            session.add(run0)
            await session.flush()
            session.add(
                RunEvent(
                    run_id=run0.run_id, job_id=job.job_id, event_type="CREATED", status_to="PENDING"
                )
            )
    return job, run0


async def _make_chained(
    factory: async_sessionmaker, *, user_id: str, trigger_on_job_id: int, description: str
) -> Job:
    """Create a trigger-driven Job (ANY, no cron, NO initial run — continuation)."""
    now = datetime.now(tz=UTC)
    async with factory() as session:
        async with session.begin():
            job = Job(
                user_id=user_id,
                description=description,
                action="capturing_chain",
                action_params={},
                job_type="one_shot",
                scheduled_at=now,
                trigger_on_job_id=trigger_on_job_id,
                trigger_on_status="ANY",
            )
            session.add(job)
            await session.flush()
            job_id = job.job_id
    async with factory() as session:
        async with session.begin():
            return (await session.execute(select(Job).where(Job.job_id == job_id))).scalar_one()


# ---------------------------------------------------------------------------
# 1. Per-tick re-fire is idempotent under redelivery (no double-create).
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_continuation_refires_every_tick_and_is_idempotent(session_factory, sqs):
    """Recurring A → B re-fires per tick, and a redelivered terminal event never doubles.

    Each tick: complete A, poll (creates A(next) + B), then redeliver the same terminal
    event (cursor cleared) and poll again — exactly-once must hold (no second A(next),
    no second B run). One FAILED tick (trigger_on_status=ANY) proves a failed tick still
    fires and the next tick still fires.
    """
    user = "continuation-idempotent-test"
    job_a, run_a0 = await _make_recurring_root(session_factory, user_id=user)
    job_b = await _make_chained(
        session_factory, user_id=user, trigger_on_job_id=job_a.job_id, description="B"
    )

    a_run_ids: list[int] = []
    b_run_ids: list[int] = []
    a_current = run_a0
    fail_tick = 1

    for tick in range(3):
        status = "FAILED" if tick == fail_tick else "SUCCEEDED"
        event_id = await _terminate_run(
            session_factory, a_current, status=status, result={"tick": tick}
        )
        a_run_ids.append(a_current.run_id)

        assert await continuation_poll_once(session_factory) >= 1
        # Redelivery: same terminal event reprocessed must not double-create.
        await _redeliver(session_factory, event_id)

        b_pending = await _runs_by_status(session_factory, job_b.job_id, "PENDING")
        assert len(b_pending) == 1, (
            f"tick {tick}: exactly one B run per tick (idempotent), got {len(b_pending)}"
        )
        assert b_pending[0].wait_for_run_id == a_current.run_id
        b_run_ids.append(b_pending[0].run_id)

        a_pending = await _runs_by_status(session_factory, job_a.job_id, "PENDING")
        assert len(a_pending) == 1, (
            f"tick {tick}: exactly one A successor per tick (idempotent), got {len(a_pending)}"
        )

        # Execute B so it is not a slow consumer next tick.
        await process_one(
            session_factory,
            sqs,
            _sqs_message(b_pending[0].run_id, job_b.job_id),
            registry=_REGISTRY,
        )
        if tick < 2:
            a_current = a_pending[0]

    async with session_factory() as session:
        async with session.begin():
            b_succeeded = (
                (
                    await session.execute(
                        select(JobRun).where(
                            JobRun.job_id == job_b.job_id, JobRun.status == "SUCCEEDED"
                        )
                    )
                )
                .scalars()
                .all()
            )
    assert len(b_succeeded) == 3, f"expected 3 SUCCEEDED B runs, got {len(b_succeeded)}"
    by_id = {r.run_id: json.loads(r.result) for r in b_succeeded}
    for tick in range(3):
        assert by_id[b_run_ids[tick]]["received_from_run_id"] == a_run_ids[tick]


# ---------------------------------------------------------------------------
# 2. A downstream that never drains has its overlapping ticks skipped.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_slow_consumer_skip_persists_across_ticks(session_factory):
    """If B never executes, every overlapping upstream tick is skipped, never created.

    Drive 3 recurring A ticks without ever executing B. Only the first tick creates B's
    run; the next two find B still executing and skip-create with an audit event. At no
    point do two executing B runs coexist (continuation slow-consumer, ADR-067 §6).
    """
    user = "slow-consumer-multitick"
    job_a, run_a0 = await _make_recurring_root(session_factory, user_id=user)
    job_b = await _make_chained(
        session_factory, user_id=user, trigger_on_job_id=job_a.job_id, description="B"
    )

    a_current = run_a0
    for tick in range(3):
        await _terminate_run(session_factory, a_current, status="SUCCEEDED", result={"tick": tick})
        assert await continuation_poll_once(session_factory) >= 1
        # B is never executed → it stays an executing PENDING run.
        executing = [
            r
            for r in await _all_runs(session_factory, job_b.job_id)
            if r.status in ("PENDING", "QUEUED", "RUNNING", "RETRYING")
        ]
        assert len(executing) == 1, (
            f"tick {tick}: at most one executing B run, got {len(executing)}"
        )
        if tick < 2:
            a_current = (await _runs_by_status(session_factory, job_a.job_id, "PENDING"))[0]

    all_b = await _all_runs(session_factory, job_b.job_id)
    assert len(all_b) == 1, f"only one B run ever created across 3 ticks, got {len(all_b)}"
    assert all_b[0].status == "PENDING", "the run was never cancelled — skip-create, not drop"

    async with session_factory() as session:
        async with session.begin():
            drops = (
                (
                    await session.execute(
                        select(RunEvent).where(RunEvent.event_type == "CHAIN_SKIPPED_SLOW_CONSUMER")
                    )
                )
                .scalars()
                .all()
            )
    assert len(drops) == 2, f"expected 2 slow-consumer skips (ticks 1 and 2), got {len(drops)}"


# ---------------------------------------------------------------------------
# 3. Fan-out cascade re-fires every tick under continuation.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_fan_out_cascade_refires_under_continuation(session_factory, sqs):
    """root → mid → (sink1 + sink2) re-fires every tick; each run reads its own upstream."""
    user = "continuation-fanout-test"
    root, run_root0 = await _make_recurring_root(session_factory, user_id=user)
    mid = await _make_chained(
        session_factory, user_id=user, trigger_on_job_id=root.job_id, description="mid"
    )
    sink1 = await _make_chained(
        session_factory, user_id=user, trigger_on_job_id=mid.job_id, description="sink1"
    )
    sink2 = await _make_chained(
        session_factory, user_id=user, trigger_on_job_id=mid.job_id, description="sink2"
    )

    mid_run_ids: list[int] = []
    sink1_run_ids: list[int] = []
    sink2_run_ids: list[int] = []
    root_current = run_root0

    for tick in range(3):
        await _terminate_run(session_factory, root_current, status="SUCCEEDED", result={"t": tick})
        assert await continuation_poll_once(session_factory) >= 1

        mid_run = (await _runs_by_status(session_factory, mid.job_id, "PENDING"))[0]
        assert mid_run.wait_for_run_id == root_current.run_id
        mid_run_ids.append(mid_run.run_id)
        await process_one(
            session_factory, sqs, _sqs_message(mid_run.run_id, mid.job_id), registry=_REGISTRY
        )

        assert await continuation_poll_once(session_factory) >= 1
        s1_run = (await _runs_by_status(session_factory, sink1.job_id, "PENDING"))[0]
        s2_run = (await _runs_by_status(session_factory, sink2.job_id, "PENDING"))[0]
        assert s1_run.wait_for_run_id == mid_run.run_id
        assert s2_run.wait_for_run_id == mid_run.run_id
        for run, job_id, ids in [
            (s1_run, sink1.job_id, sink1_run_ids),
            (s2_run, sink2.job_id, sink2_run_ids),
        ]:
            await process_one(
                session_factory, sqs, _sqs_message(run.run_id, job_id), registry=_REGISTRY
            )
            ids.append(run.run_id)

        if tick < 2:
            root_current = (await _runs_by_status(session_factory, root.job_id, "PENDING"))[0]

    async with session_factory() as session:
        async with session.begin():
            s1 = (
                (
                    await session.execute(
                        select(JobRun).where(
                            JobRun.job_id == sink1.job_id, JobRun.status == "SUCCEEDED"
                        )
                    )
                )
                .scalars()
                .all()
            )
            s2 = (
                (
                    await session.execute(
                        select(JobRun).where(
                            JobRun.job_id == sink2.job_id, JobRun.status == "SUCCEEDED"
                        )
                    )
                )
                .scalars()
                .all()
            )
    assert len(s1) == 3 and len(s2) == 3
    s1_by = {r.run_id: json.loads(r.result) for r in s1}
    s2_by = {r.run_id: json.loads(r.result) for r in s2}
    for tick in range(3):
        assert s1_by[sink1_run_ids[tick]]["received_from_run_id"] == mid_run_ids[tick]
        assert s2_by[sink2_run_ids[tick]]["received_from_run_id"] == mid_run_ids[tick]
