"""Integration test: continuation run-creation + exactly-once (ADR-067, issue #254).

Drives the REAL continuation path — no faked downstream-run insert (CODING_STANDARDS
anti-pattern #10). A trigger-driven downstream run is created by the continuation
consumer when its upstream run reaches a terminal status; nothing is pre-armed.

Topology (recurring chain):
  Job A: recurring (cron "@hourly") — the root; produces a result per tick.
  Job B: trigger-driven (trigger_on_job_id=A, trigger_on_status="ANY") — NO cron,
         and NO initial run (continuation; it is created on A's terminal event).

Per-tick flow:
  1. Mark A's current run SUCCEEDED + emit RunEvent(SUCCEEDED).
  2. continuation poll_once: materialize_successor spawns A(next) PENDING AND
     materialize_downstream spawns B's PENDING run (wait_for_run_id = this tick's A
     run), both in one transaction.
  3. executor.process_one(B_run): injects from_run_id from wait_for_run_id.
  4. Assert B_run.result["received_from_run_id"] == that tick's A run_id.

Plus the S3 guarantees: exactly-once on redelivery (unique constraint), slow-consumer
skip-create with audit, and predicate-miss no-create with audit.

Run with:
    uv run pytest -m integration tests/integration/test_chain_recurring.py
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

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
            ok=True,
            result={"received_from_run_id": params.from_run_id},
            error=None,
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
) -> RunEvent:
    """Mark a run terminal + emit the matching RunEvent. Returns the committed event."""
    now = datetime.now(tz=UTC)
    async with factory() as session:
        async with session.begin():
            await session.execute(
                update(JobRun)
                .where(JobRun.run_id == run.run_id)
                .values(
                    status=status,
                    finish_at=now,
                    result=json.dumps(result) if result is not None else None,
                )
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
            event_id = event.event_id
    async with factory() as session:
        async with session.begin():
            return (
                await session.execute(select(RunEvent).where(RunEvent.event_id == event_id))
            ).scalar_one()


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


async def _events_by_type(factory: async_sessionmaker, event_type: str) -> list[RunEvent]:
    async with factory() as session:
        async with session.begin():
            return (
                (await session.execute(select(RunEvent).where(RunEvent.event_type == event_type)))
                .scalars()
                .all()
            )


async def _clear_cursor(factory: async_sessionmaker, event_id: int) -> None:
    """Simulate a crash-before-stamp redelivery: wipe the processed_by cursor."""
    async with factory() as session:
        async with session.begin():
            await session.execute(
                update(RunEvent).where(RunEvent.event_id == event_id).values(processed_by={})
            )


async def _make_recurring_root(factory: async_sessionmaker, *, user_id: str) -> tuple[Job, JobRun]:
    """Create a recurring root Job A with its initial PENDING run + CREATED event."""
    now = datetime.now(tz=UTC)
    bucket = now.replace(minute=0, second=0, microsecond=0).isoformat()
    async with factory() as session:
        async with session.begin():
            job_a = Job(
                user_id=user_id,
                description="upstream recurring A",
                action="capturing_chain",
                action_params={},
                job_type="recurring",
                cron_expr="@hourly",
                timezone="UTC",
            )
            session.add(job_a)
            await session.flush()
            run_a0 = JobRun(
                time_bucket=bucket,
                job_id=job_a.job_id,
                user_id=job_a.user_id,
                scheduled_at=now,
                status="PENDING",
            )
            session.add(run_a0)
            await session.flush()
            session.add(
                RunEvent(
                    run_id=run_a0.run_id,
                    job_id=job_a.job_id,
                    event_type="CREATED",
                    status_from=None,
                    status_to="PENDING",
                )
            )
    return job_a, run_a0


async def _make_chained(
    factory: async_sessionmaker,
    *,
    user_id: str,
    trigger_on_job_id: int,
    trigger_on_status: str,
    description: str = "downstream B",
) -> Job:
    """Create a trigger-driven Job (no cron, NO initial run — continuation)."""
    now = datetime.now(tz=UTC)
    async with factory() as session:
        async with session.begin():
            job = Job(
                user_id=user_id,
                description=description,
                action="capturing_chain",
                action_params={},
                job_type="one_shot",
                scheduled_at=now,  # required by DB constraint for one_shot
                trigger_on_job_id=trigger_on_job_id,
                trigger_on_status=trigger_on_status,
            )
            session.add(job)
            await session.flush()
            job_id = job.job_id
    async with factory() as session:
        async with session.begin():
            return (await session.execute(select(Job).where(Job.job_id == job_id))).scalar_one()


# ---------------------------------------------------------------------------
# Core test: 3 ticks — each B run reads its own tick's A run_id
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_recurring_chain_refires_per_tick_3_ticks(session_factory, sqs):
    """Recurring A (cron) → chained B (NO cron, NO pre-armed run): B refires per A tick.

    Each B run is CREATED on its tick's A terminal event (continuation) and reads
    its own tick's A run_id via from_run_id.
    """
    user = "chain-recurring-test"
    job_a, run_a0 = await _make_recurring_root(session_factory, user_id=user)
    job_b = await _make_chained(
        session_factory,
        user_id=user,
        trigger_on_job_id=job_a.job_id,
        trigger_on_status="ANY",
    )

    # No B run exists until A terminates (continuation).
    assert await _all_runs(session_factory, job_b.job_id) == []

    a_run_ids_per_tick: list[int] = []
    b_run_ids_per_tick: list[int] = []
    a_current = run_a0

    for tick in range(3):
        # Step 1: complete A's current run.
        await _terminate_run(
            session_factory, a_current, result={"tick": tick, "data": f"tick-{tick}"}
        )
        a_run_ids_per_tick.append(a_current.run_id)

        # Step 2: continuation consumer creates A(next) + B's run (one transaction).
        count = await continuation_poll_once(session_factory)
        assert count >= 1, f"tick {tick}: continuation consumer processed 0 events"

        # B has exactly one PENDING run, pointing at THIS tick's A run.
        b_pending = await _runs_by_status(session_factory, job_b.job_id, "PENDING")
        assert len(b_pending) == 1, (
            f"tick {tick}: expected 1 PENDING B run (continuation), got {len(b_pending)}"
        )
        b_run = b_pending[0]
        assert b_run.wait_for_run_id == a_current.run_id, (
            f"tick {tick}: B run wait_for_run_id={b_run.wait_for_run_id}"
            f" != this tick's A run_id={a_current.run_id}"
        )
        b_run_ids_per_tick.append(b_run.run_id)

        # Step 3: execute B's run; capturing handler stores from_run_id in result.
        await process_one(
            session_factory, sqs, _sqs_message(b_run.run_id, job_b.job_id), registry=_REGISTRY
        )

        # Advance to A's freshly-materialised successor for the next tick.
        if tick < 2:
            a_pending = await _runs_by_status(session_factory, job_a.job_id, "PENDING")
            assert len(a_pending) == 1, (
                f"tick {tick}: expected 1 new PENDING A run, got {len(a_pending)}"
            )
            a_current = a_pending[0]

    # Each B run received its own tick's A run_id.
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
    by_run_id = {r.run_id: json.loads(r.result) for r in b_succeeded}
    for tick in range(3):
        received = by_run_id[b_run_ids_per_tick[tick]]["received_from_run_id"]
        assert received == a_run_ids_per_tick[tick], (
            f"tick {tick}: B received from_run_id={received},"
            f" expected this tick's A run_id={a_run_ids_per_tick[tick]}"
        )


# ---------------------------------------------------------------------------
# Exactly-once: a redelivered terminal event must not double-create
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_exactly_once_redelivered_terminal_does_not_double_create(session_factory, sqs):
    """A redelivered upstream terminal event creates the downstream run only once.

    Drives the (job_id, wait_for_run_id) unique index: after the downstream has run
    to completion, reprocessing the same terminal event (cursor cleared = crash
    before stamp) must NOT create a second downstream run.
    """
    user = "exactly-once-test"
    now = datetime.now(tz=UTC)
    bucket = now.replace(minute=0, second=0, microsecond=0).isoformat()

    # One-shot A (no successor) + chained B.
    async with session_factory() as session:
        async with session.begin():
            job_a = Job(
                user_id=user,
                description="one-shot A",
                action="capturing_chain",
                action_params={},
                job_type="one_shot",
                scheduled_at=now,
            )
            session.add(job_a)
            await session.flush()
            run_a = JobRun(
                time_bucket=bucket,
                job_id=job_a.job_id,
                user_id=user,
                scheduled_at=now,
                status="PENDING",
            )
            session.add(run_a)
            await session.flush()
            session.add(
                RunEvent(
                    run_id=run_a.run_id,
                    job_id=job_a.job_id,
                    event_type="CREATED",
                    status_to="PENDING",
                )
            )
            a_job_id = job_a.job_id
    job_b = await _make_chained(
        session_factory, user_id=user, trigger_on_job_id=a_job_id, trigger_on_status="ANY"
    )

    # Terminate A; first poll creates B's run; run B to completion.
    event_a = await _terminate_run(session_factory, run_a, result={"x": 1})
    assert await continuation_poll_once(session_factory) >= 1
    b_pending = await _runs_by_status(session_factory, job_b.job_id, "PENDING")
    assert len(b_pending) == 1
    await process_one(
        session_factory, sqs, _sqs_message(b_pending[0].run_id, job_b.job_id), registry=_REGISTRY
    )
    assert len(await _runs_by_status(session_factory, job_b.job_id, "SUCCEEDED")) == 1

    # Redelivery: clear the cursor and reprocess A's terminal event.
    await _clear_cursor(session_factory, event_a.event_id)
    await continuation_poll_once(session_factory)

    all_b = await _all_runs(session_factory, job_b.job_id)
    assert len(all_b) == 1, (
        f"exactly-once: a redelivered terminal event must not double-create B,"
        f" got {len(all_b)} runs: {[(r.run_id, r.status) for r in all_b]}"
    )


# ---------------------------------------------------------------------------
# Slow consumer: downstream already executing → skip-create + audit
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_slow_consumer_skips_create_with_audit(session_factory):
    """An upstream that outpaces a still-executing downstream drops the tick (no create).

    Recurring A produces two terminal events while B's first run is still executing.
    The second event must NOT create a second B run; it records a
    CHAIN_SKIPPED_SLOW_CONSUMER audit event (no created-then-cancelled run).
    """
    user = "slow-consumer-test"
    job_a, run_a0 = await _make_recurring_root(session_factory, user_id=user)
    job_b = await _make_chained(
        session_factory, user_id=user, trigger_on_job_id=job_a.job_id, trigger_on_status="ANY"
    )

    # Tick 0: complete A; poll creates A(next) + B run0 (left PENDING = executing).
    await _terminate_run(session_factory, run_a0, result={"tick": 0})
    assert await continuation_poll_once(session_factory) >= 1
    b_runs_0 = await _runs_by_status(session_factory, job_b.job_id, "PENDING")
    assert len(b_runs_0) == 1
    a_next = (await _runs_by_status(session_factory, job_a.job_id, "PENDING"))[0]

    # Tick 1: complete A(next); poll sees B still executing → slow-consumer skip.
    await _terminate_run(session_factory, a_next, result={"tick": 1})
    assert await continuation_poll_once(session_factory) >= 1

    all_b = await _all_runs(session_factory, job_b.job_id)
    assert len(all_b) == 1, (
        f"slow consumer: the overlapping tick must be skipped (not created), got {len(all_b)}"
    )
    assert all_b[0].status == "PENDING", "the existing executing B run is untouched (not cancelled)"
    drops = await _events_by_type(session_factory, "CHAIN_SKIPPED_SLOW_CONSUMER")
    assert len(drops) == 1, "slow-consumer skip must record exactly one audit event"
    assert drops[0].event_data["downstream_job_id"] == job_b.job_id


# ---------------------------------------------------------------------------
# Predicate miss: trigger_on_status not satisfied → no run + audit
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_predicate_miss_creates_no_run_with_audit(session_factory):
    """trigger_on_status=SUCCEEDED + a FAILED upstream creates no downstream run.

    No CANCELLED_BY_CHAIN_MISS run is produced (continuation never creates one to
    cancel); a lightweight CHAIN_SKIPPED_PREDICATE_MISS audit event is recorded.
    """
    user = "predicate-miss-test"
    now = datetime.now(tz=UTC)
    bucket = now.replace(minute=0, second=0, microsecond=0).isoformat()

    async with session_factory() as session:
        async with session.begin():
            job_a = Job(
                user_id=user,
                description="one-shot A",
                action="capturing_chain",
                action_params={},
                job_type="one_shot",
                scheduled_at=now,
            )
            session.add(job_a)
            await session.flush()
            run_a = JobRun(
                time_bucket=bucket,
                job_id=job_a.job_id,
                user_id=user,
                scheduled_at=now,
                status="PENDING",
            )
            session.add(run_a)
            await session.flush()
            session.add(
                RunEvent(
                    run_id=run_a.run_id,
                    job_id=job_a.job_id,
                    event_type="CREATED",
                    status_to="PENDING",
                )
            )
            a_job_id = job_a.job_id
    job_b = await _make_chained(
        session_factory, user_id=user, trigger_on_job_id=a_job_id, trigger_on_status="SUCCEEDED"
    )

    # A FAILS; SUCCEEDED predicate is not satisfied.
    await _terminate_run(session_factory, run_a, status="FAILED")
    assert await continuation_poll_once(session_factory) >= 1

    assert await _all_runs(session_factory, job_b.job_id) == [], (
        "predicate miss must create no downstream run"
    )
    assert await _runs_by_status(session_factory, job_b.job_id, "CANCELLED") == [], (
        "no CANCELLED_BY_CHAIN_MISS run may be produced under continuation"
    )
    misses = await _events_by_type(session_factory, "CHAIN_SKIPPED_PREDICATE_MISS")
    assert len(misses) == 1, "predicate miss must record exactly one audit event"
    assert misses[0].event_data["downstream_job_id"] == job_b.job_id


# ---------------------------------------------------------------------------
# One-shot chain regression: explicit from_run_id must NOT be overwritten
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_one_shot_chain_explicit_from_run_id_not_overwritten(session_factory, sqs):
    """One-shot chain: when from_run_id is set in params, the executor must not override it.

    The injection only applies when from_run_id is None (unset) — not when the
    caller hard-coded a specific upstream run_id.
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

    await process_one(
        session_factory, sqs, _sqs_message(run_b.run_id, job_b.job_id), registry=_REGISTRY
    )

    async with session_factory() as session:
        async with session.begin():
            updated_run = (
                await session.execute(select(JobRun).where(JobRun.run_id == run_b.run_id))
            ).scalar_one()

    assert updated_run.status == "SUCCEEDED"
    result = json.loads(updated_run.result)
    assert result["received_from_run_id"] == run_a_original.run_id, (
        f"expected original from_run_id={run_a_original.run_id},"
        f" got {result['received_from_run_id']} (wait_for_run_id={run_a_later.run_id})"
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
                action_params={},
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
                wait_for_run_id=None,
            )
            session.add(run)

    await process_one(
        session_factory, sqs, _sqs_message(run.run_id, job.job_id), registry=_REGISTRY
    )

    async with session_factory() as session:
        async with session.begin():
            updated_run = (
                await session.execute(select(JobRun).where(JobRun.run_id == run.run_id))
            ).scalar_one()

    assert updated_run.status == "SUCCEEDED"
    result = json.loads(updated_run.result)
    assert result["received_from_run_id"] is None


# ---------------------------------------------------------------------------
# Full fan-out digest shape over multiple ticks (continuation)
# ---------------------------------------------------------------------------
#
#   root (cron "@hourly")  →  mid (trigger_on=root, ANY)  →  sink1 + sink2 (trigger_on=mid)
#
# Per tick: complete root → poll creates root(next) + mid; execute mid → its terminal
# event drives the next poll, which creates sink1 + sink2; execute both sinks. Each run
# reads its own tick's direct-upstream run_id (mid←root, sinks←mid).


@pytest.mark.integration
async def test_fan_out_digest_shape_refires_per_tick(session_factory, sqs):
    """root→mid→(sink1+sink2) re-fires end to end per tick under continuation."""
    user = "fanout-digest-test"
    job_root, run_root0 = await _make_recurring_root(session_factory, user_id=user)
    job_mid = await _make_chained(
        session_factory,
        user_id=user,
        trigger_on_job_id=job_root.job_id,
        trigger_on_status="ANY",
        description="mid",
    )
    job_sink1 = await _make_chained(
        session_factory,
        user_id=user,
        trigger_on_job_id=job_mid.job_id,
        trigger_on_status="ANY",
        description="sink1",
    )
    job_sink2 = await _make_chained(
        session_factory,
        user_id=user,
        trigger_on_job_id=job_mid.job_id,
        trigger_on_status="ANY",
        description="sink2",
    )

    root_run_ids: list[int] = []
    mid_run_ids: list[int] = []
    sink1_run_ids: list[int] = []
    sink2_run_ids: list[int] = []
    root_current = run_root0

    for tick in range(3):
        # Complete root → poll creates root(next) + mid's run.
        await _terminate_run(session_factory, root_current, result={"tick": tick, "src": "root"})
        root_run_ids.append(root_current.run_id)
        assert await continuation_poll_once(session_factory) >= 1

        mid_pending = await _runs_by_status(session_factory, job_mid.job_id, "PENDING")
        assert len(mid_pending) == 1, f"tick {tick}: expected 1 PENDING mid run"
        mid_run = mid_pending[0]
        assert mid_run.wait_for_run_id == root_current.run_id
        mid_run_ids.append(mid_run.run_id)

        # Execute mid → its terminal event drives sink creation on the next poll.
        await process_one(
            session_factory, sqs, _sqs_message(mid_run.run_id, job_mid.job_id), registry=_REGISTRY
        )
        async with session_factory() as session:
            async with session.begin():
                mid_done = (
                    await session.execute(select(JobRun).where(JobRun.run_id == mid_run.run_id))
                ).scalar_one()
        assert mid_done.status == "SUCCEEDED"
        assert json.loads(mid_done.result)["received_from_run_id"] == root_current.run_id

        # Poll: mid's terminal event fans out to sink1 + sink2.
        assert await continuation_poll_once(session_factory) >= 1
        sink1_pending = await _runs_by_status(session_factory, job_sink1.job_id, "PENDING")
        sink2_pending = await _runs_by_status(session_factory, job_sink2.job_id, "PENDING")
        assert len(sink1_pending) == 1, f"tick {tick}: expected 1 PENDING sink1 run"
        assert len(sink2_pending) == 1, f"tick {tick}: expected 1 PENDING sink2 run"
        assert sink1_pending[0].wait_for_run_id == mid_run.run_id
        assert sink2_pending[0].wait_for_run_id == mid_run.run_id

        for sink_run, sink_job_id, ids in [
            (sink1_pending[0], job_sink1.job_id, sink1_run_ids),
            (sink2_pending[0], job_sink2.job_id, sink2_run_ids),
        ]:
            await process_one(
                session_factory, sqs, _sqs_message(sink_run.run_id, sink_job_id), registry=_REGISTRY
            )
            ids.append(sink_run.run_id)

        if tick < 2:
            root_pending = await _runs_by_status(session_factory, job_root.job_id, "PENDING")
            assert len(root_pending) == 1, f"tick {tick}: expected 1 new PENDING root run"
            root_current = root_pending[0]

    # Each sink run read its own tick's mid run_id.
    async with session_factory() as session:
        async with session.begin():
            sink1_done = (
                (
                    await session.execute(
                        select(JobRun).where(
                            JobRun.job_id == job_sink1.job_id, JobRun.status == "SUCCEEDED"
                        )
                    )
                )
                .scalars()
                .all()
            )
            sink2_done = (
                (
                    await session.execute(
                        select(JobRun).where(
                            JobRun.job_id == job_sink2.job_id, JobRun.status == "SUCCEEDED"
                        )
                    )
                )
                .scalars()
                .all()
            )
    assert len(sink1_done) == 3 and len(sink2_done) == 3
    s1 = {r.run_id: json.loads(r.result) for r in sink1_done}
    s2 = {r.run_id: json.loads(r.result) for r in sink2_done}
    for tick in range(3):
        assert s1[sink1_run_ids[tick]]["received_from_run_id"] == mid_run_ids[tick]
        assert s2[sink2_run_ids[tick]]["received_from_run_id"] == mid_run_ids[tick]


# ---------------------------------------------------------------------------
# Self-healing: a FAILED upstream tick still drives the full downstream cascade
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_fan_out_self_heals_on_failed_upstream_tick(session_factory, sqs):
    """A FAILED root (trigger_on_status=ANY) still creates + drives the whole cascade.

    The chain is wired ANY, so a FAILED root must NOT stall the digest: continuation
    creates mid (reading the failed root's run_id for its fallback), and mid's terminal
    event then creates both sinks.
    """
    user = "self-heal-test"
    job_root, run_root0 = await _make_recurring_root(session_factory, user_id=user)
    job_mid = await _make_chained(
        session_factory,
        user_id=user,
        trigger_on_job_id=job_root.job_id,
        trigger_on_status="ANY",
        description="mid",
    )
    job_sink1 = await _make_chained(
        session_factory,
        user_id=user,
        trigger_on_job_id=job_mid.job_id,
        trigger_on_status="ANY",
        description="sink1",
    )
    job_sink2 = await _make_chained(
        session_factory,
        user_id=user,
        trigger_on_job_id=job_mid.job_id,
        trigger_on_status="ANY",
        description="sink2",
    )

    # Root FAILS this tick.
    await _terminate_run(session_factory, run_root0, status="FAILED")
    assert await continuation_poll_once(session_factory) >= 1

    mid_pending = await _runs_by_status(session_factory, job_mid.job_id, "PENDING")
    assert len(mid_pending) == 1, "a FAILED root (ANY) must still create the mid run"
    assert mid_pending[0].wait_for_run_id == run_root0.run_id

    await process_one(
        session_factory,
        sqs,
        _sqs_message(mid_pending[0].run_id, job_mid.job_id),
        registry=_REGISTRY,
    )
    async with session_factory() as session:
        async with session.begin():
            mid_done = (
                await session.execute(select(JobRun).where(JobRun.run_id == mid_pending[0].run_id))
            ).scalar_one()
    assert mid_done.status == "SUCCEEDED"
    assert json.loads(mid_done.result)["received_from_run_id"] == run_root0.run_id

    assert await continuation_poll_once(session_factory) >= 1
    sink1_pending = await _runs_by_status(session_factory, job_sink1.job_id, "PENDING")
    sink2_pending = await _runs_by_status(session_factory, job_sink2.job_id, "PENDING")
    assert len(sink1_pending) == 1 and len(sink2_pending) == 1, (
        "both sinks must be driven after a failed upstream tick"
    )
    for sink_run, sink_job_id in [
        (sink1_pending[0], job_sink1.job_id),
        (sink2_pending[0], job_sink2.job_id),
    ]:
        await process_one(
            session_factory, sqs, _sqs_message(sink_run.run_id, sink_job_id), registry=_REGISTRY
        )

    assert len(await _runs_by_status(session_factory, job_sink1.job_id, "SUCCEEDED")) == 1
    assert len(await _runs_by_status(session_factory, job_sink2.job_id, "SUCCEEDED")) == 1
