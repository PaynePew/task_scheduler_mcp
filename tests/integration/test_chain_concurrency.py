"""Integration tests: per-tick re-fire under REAL watcher concurrency (issue #234).

The merged suites in ``test_chain_recurring.py`` drive the watchers in lockstep —
flip the downstream WAITING run, execute it to completion, and only *then* run the
recurring poll. That is the single ordering that works on the pre-#234 invariant.

Real deployments run ``RecurringJobWatcher`` and ``ChainWatcher`` concurrently:
each consumes the same upstream terminal event independently within one ~5 s poll.
The recurring poll routinely fires FIRST — arming tick N+1's downstream WAITING run
while tick N's downstream run is still un-flipped (WAITING) or in flight
(PENDING/RUNNING). Under the old "at most one *non-terminal* run" invariant, the
spawn-time forbid-concurrency check counted that live downstream run and raised
``ConcurrencyError``, so ``_arm`` silently skipped the entire downstream subtree and
no WAITING run existed for the next tick — the chain went dark every other day.

ADR-065 §4 (revised by #234) redefines the invariant as **at most one *executing*
run** (PENDING/QUEUED/RUNNING/RETRYING). Arming is unconditional per tick; the
flip-time ``CANCELLED_SLOW_CONSUMER`` drop is the single audited load-shedding path.

These tests reproduce the reversed (recurring-poll-first) ordering and assert the
chain re-fires every tick, each downstream run reading ITS OWN tick's upstream
run_id. They fail on the pre-#234 code and pass after it.

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
from app.workers.chain_watcher import poll_once as chain_poll_once
from app.workers.executor import process_one
from app.workers.recurring_watcher import poll_once as recurring_poll_once

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
    status: str,
    result: dict | None = None,
) -> None:
    """Mark *run* terminal (SUCCEEDED/FAILED) + emit the matching RunEvent."""
    now = datetime.now(tz=UTC)
    async with factory() as session:
        async with session.begin():
            values: dict = {"status": status, "finish_at": now}
            if result is not None:
                values["result"] = json.dumps(result)
            await session.execute(
                update(JobRun).where(JobRun.run_id == run.run_id).values(**values)
            )
            session.add(
                RunEvent(
                    run_id=run.run_id,
                    job_id=run.job_id,
                    event_type=status,
                    status_from="RUNNING",
                    status_to=status,
                    occurred_at=now,
                )
            )


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


async def _seed_linear_chain(
    factory: async_sessionmaker,
) -> tuple[Job, Job, JobRun, JobRun]:
    """Seed root (recurring A) → downstream (trigger-driven B, no cron), plus initial runs.

    Returns (job_a, job_b, run_a0, run_b0_waiting).
    """
    now = datetime.now(tz=UTC)
    bucket = now.replace(minute=0, second=0, microsecond=0).isoformat()
    async with factory() as session:
        async with session.begin():
            job_a = Job(
                user_id="reversed-order-test",
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

            job_b = Job(
                user_id="reversed-order-test",
                description="downstream trigger-driven B (no cron)",
                action="capturing_chain",
                action_params={},
                job_type="one_shot",
                scheduled_at=now,
                trigger_on_job_id=job_a.job_id,
                trigger_on_status="ANY",
            )
            session.add(job_b)
            await session.flush()

            run_b0 = JobRun(
                time_bucket=bucket,
                job_id=job_b.job_id,
                user_id=job_b.user_id,
                scheduled_at=now,
                status="WAITING",
                wait_for_run_id=run_a0.run_id,
            )
            session.add(run_b0)
            await session.flush()
            session.add(
                RunEvent(
                    run_id=run_b0.run_id,
                    job_id=job_b.job_id,
                    event_type="CREATED",
                    status_from=None,
                    status_to="WAITING",
                )
            )
    return job_a, job_b, run_a0, run_b0


# ---------------------------------------------------------------------------
# Reversed-order regression: recurring poll FIRST, then flip, then execute.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_recurring_poll_first_still_refires_every_tick(session_factory, sqs):
    """Reversed order (recurring-poll-FIRST) must still re-fire B every tick.

    This is the ordering that fails on pre-#234 main: per tick we complete A's run,
    then run the RECURRING watcher FIRST — so ``_arm`` runs while B's current-tick
    WAITING run is still un-flipped — then the chain watcher flip, then execute B.

    Under the old non-terminal predicate, ``_arm`` saw B's WAITING run as "live" and
    skipped, leaving no WAITING run for the next tick. After #234 (executing-only
    predicate) arming is unconditional, so every tick produces a B run that reads ITS
    OWN tick's A run_id.

    One tick uses a FAILED root with trigger_on_status=ANY: a failed tick must still
    arm AND the next tick must still fire (the across-ticks self-healing gap #232 left).
    """
    job_a, job_b, run_a0, run_b0 = await _seed_linear_chain(session_factory)

    a_run_ids: list[int] = []
    b_run_ids: list[int] = []
    a_current = run_a0
    b_current_waiting = run_b0

    # Tick 1 fails the root (trigger_on_status=ANY) → proves a FAILED tick still arms
    # AND the next tick still fires.
    fail_tick = 1

    for tick in range(3):
        # --- Step 1: complete A's current run (terminal event emitted) ---
        if tick == fail_tick:
            await _terminate_run(session_factory, a_current, status="FAILED")
        else:
            await _terminate_run(
                session_factory,
                a_current,
                status="SUCCEEDED",
                result={"tick": tick},
            )
        a_run_ids.append(a_current.run_id)

        # --- Step 2 (REVERSED): recurring watcher FIRST — arms tick N+1 while B's
        #     current WAITING run is still un-flipped. Pre-#234 this skips the arm. ---
        rw_count = await recurring_poll_once(session_factory)
        assert rw_count >= 1, f"tick {tick}: recurring_watcher processed 0 events"

        if tick < 2:
            # A's next PENDING run exists ...
            a_pending = await _runs_by_status(session_factory, job_a.job_id, "PENDING")
            assert len(a_pending) == 1, (
                f"tick {tick}: expected 1 new PENDING A run, got {len(a_pending)}"
            )
            next_a = a_pending[0]

            # ... and B's NEXT WAITING run was armed even though B's current WAITING
            # run is still live. Two WAITING runs for B now transiently coexist — the
            # exact state the old invariant forbade (and silently dropped).
            b_waiting = await _runs_by_status(session_factory, job_b.job_id, "WAITING")
            armed_next = [w for w in b_waiting if w.run_id != b_current_waiting.run_id]
            assert len(armed_next) == 1, (
                f"tick {tick}: expected B's next-tick WAITING run to be armed while the "
                f"current WAITING run is still live; got next-armed={len(armed_next)} "
                f"(total WAITING={[w.run_id for w in b_waiting]}). "
                "Pre-#234 the spawn-time non-terminal check skipped this arm."
            )
            assert armed_next[0].wait_for_run_id == next_a.run_id, (
                f"tick {tick}: B's next WAITING run must point at the NEW A run "
                f"{next_a.run_id}, got {armed_next[0].wait_for_run_id}"
            )

        # --- Step 3: chain watcher flips B's CURRENT WAITING run → PENDING ---
        cw_count = await chain_poll_once(session_factory)
        assert cw_count >= 1, f"tick {tick}: chain_watcher processed 0 events"

        b_pending = await _runs_by_status(session_factory, job_b.job_id, "PENDING")
        flipped = [r for r in b_pending if r.run_id == b_current_waiting.run_id]
        assert len(flipped) == 1, (
            f"tick {tick}: expected current B run {b_current_waiting.run_id} to be PENDING, "
            f"got PENDING={[r.run_id for r in b_pending]}"
        )
        b_run_ids.append(b_current_waiting.run_id)

        # --- Step 4: execute B's run; capturing handler records from_run_id ---
        await process_one(
            session_factory,
            sqs,
            _sqs_message(b_current_waiting.run_id, job_b.job_id),
            registry=_REGISTRY,
        )

        if tick < 2:
            a_current = next_a
            b_current_waiting = armed_next[0]

    # --- Final: each B run read ITS OWN tick's A run_id ---
    async with session_factory() as session:
        async with session.begin():
            b_succeeded = (
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

    assert len(b_succeeded) == 3, f"expected 3 SUCCEEDED B runs, got {len(b_succeeded)}"
    by_run_id = {r.run_id: json.loads(r.result) for r in b_succeeded}
    for tick in range(3):
        received = by_run_id[b_run_ids[tick]]["received_from_run_id"]
        assert received == a_run_ids[tick], (
            f"tick {tick}: B run {b_run_ids[tick]} read from_run_id={received}, "
            f"expected this tick's A run_id={a_run_ids[tick]}"
        )


# ---------------------------------------------------------------------------
# Slow-consumer drop through the REAL arm path (no hand-fabricated state).
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_slow_consumer_drop_through_real_arm_path(session_factory, sqs):
    """A downstream still executing when its next tick's flip arrives → CANCELLED_SLOW_CONSUMER.

    Unlike the pre-#234 slow-consumer test (which hand-built the "WAITING + another live
    run" state), this drives it through the real arm path:

      tick 0: complete A → recurring poll arms B(next) WAITING → flip B(0) → PENDING
              (B is now an *executing* run, left un-executed on purpose).
      tick 1: complete A(next) → recurring poll arms B(next+1) → flip B(1)'s WAITING run.
              B still has the executing PENDING run from tick 0, so the flip drops the
              incoming tick: WAITING → CANCELLED with a CANCELLED_SLOW_CONSUMER event.

    At no point do two *executing* runs for B coexist.
    """
    job_a, job_b, run_a0, run_b0 = await _seed_linear_chain(session_factory)

    # --- tick 0: arm B(next), flip B(0) → PENDING, but DO NOT execute it ---
    await _terminate_run(session_factory, run_a0, status="SUCCEEDED", result={"tick": 0})
    assert await recurring_poll_once(session_factory) >= 1

    a_next = (await _runs_by_status(session_factory, job_a.job_id, "PENDING"))[0]
    b_next_waiting = next(
        w
        for w in await _runs_by_status(session_factory, job_b.job_id, "WAITING")
        if w.run_id != run_b0.run_id
    )

    assert await chain_poll_once(session_factory) >= 1
    b0_pending = await _runs_by_status(session_factory, job_b.job_id, "PENDING")
    assert [r.run_id for r in b0_pending] == [run_b0.run_id], (
        f"B(0) should be the lone executing PENDING run, got {[r.run_id for r in b0_pending]}"
    )

    # --- tick 1: complete A(next), arm B(next+1), then flip B(next)'s WAITING run ---
    # B still has the executing PENDING run from tick 0 → the flip must DROP this tick.
    await _terminate_run(session_factory, a_next, status="SUCCEEDED", result={"tick": 1})
    assert await recurring_poll_once(session_factory) >= 1
    assert await chain_poll_once(session_factory) >= 1

    async with session_factory() as session:
        async with session.begin():
            dropped = (
                await session.execute(select(JobRun).where(JobRun.run_id == b_next_waiting.run_id))
            ).scalar_one()
            drop_event = (
                await session.execute(
                    select(RunEvent).where(
                        RunEvent.run_id == b_next_waiting.run_id,
                        RunEvent.event_type == "CANCELLED_SLOW_CONSUMER",
                    )
                )
            ).scalar_one_or_none()

    assert dropped.status == "CANCELLED", (
        f"incoming tick should be dropped (WAITING→CANCELLED), got {dropped.status}"
    )
    assert drop_event is not None, "expected a CANCELLED_SLOW_CONSUMER audit event"
    assert drop_event.status_from == "WAITING"
    assert drop_event.status_to == "CANCELLED"

    # At no point do two EXECUTING runs for B coexist: only B(0) PENDING survives.
    _EXECUTING = ("PENDING", "QUEUED", "RUNNING", "RETRYING")
    async with session_factory() as session:
        async with session.begin():
            executing = (
                (
                    await session.execute(
                        select(JobRun).where(
                            JobRun.job_id == job_b.job_id,
                            JobRun.status.in_(list(_EXECUTING)),
                        )
                    )
                )
                .scalars()
                .all()
            )
    assert len(executing) == 1, (
        f"at most one executing B run allowed, got {[(r.run_id, r.status) for r in executing]}"
    )
    assert executing[0].run_id == run_b0.run_id


# ---------------------------------------------------------------------------
# Fan-out cascade re-fires per tick under the reversed (recurring-poll-first) order.
# ---------------------------------------------------------------------------


async def _seed_fanout_chain(
    factory: async_sessionmaker,
) -> tuple[Job, Job, Job, Job, JobRun, JobRun, JobRun, JobRun]:
    """Seed root → mid → (sink1 + sink2), all trigger-driven (no cron) except root.

    Returns (root, mid, sink1, sink2, run_root0, run_mid0, run_sink1_0, run_sink2_0).
    """
    now = datetime.now(tz=UTC)
    bucket = now.replace(minute=0, second=0, microsecond=0).isoformat()

    def _waiting(job: Job, wait_for: int) -> JobRun:
        return JobRun(
            time_bucket=bucket,
            job_id=job.job_id,
            user_id=job.user_id,
            scheduled_at=now,
            status="WAITING",
            wait_for_run_id=wait_for,
        )

    async with factory() as session:
        async with session.begin():
            root = Job(
                user_id="reversed-fanout-test",
                description="root recurring",
                action="capturing_chain",
                action_params={},
                job_type="recurring",
                cron_expr="@hourly",
                timezone="UTC",
            )
            session.add(root)
            await session.flush()
            run_root0 = JobRun(
                time_bucket=bucket,
                job_id=root.job_id,
                user_id=root.user_id,
                scheduled_at=now,
                status="PENDING",
            )
            session.add(run_root0)
            await session.flush()
            session.add(
                RunEvent(
                    run_id=run_root0.run_id,
                    job_id=root.job_id,
                    event_type="CREATED",
                    status_from=None,
                    status_to="PENDING",
                )
            )

            mid = Job(
                user_id="reversed-fanout-test",
                description="mid",
                action="capturing_chain",
                action_params={},
                job_type="one_shot",
                scheduled_at=now,
                trigger_on_job_id=root.job_id,
                trigger_on_status="ANY",
            )
            session.add(mid)
            await session.flush()
            run_mid0 = _waiting(mid, run_root0.run_id)
            session.add(run_mid0)
            await session.flush()
            session.add(
                RunEvent(
                    run_id=run_mid0.run_id,
                    job_id=mid.job_id,
                    event_type="CREATED",
                    status_from=None,
                    status_to="WAITING",
                )
            )

            sink1 = Job(
                user_id="reversed-fanout-test",
                description="sink1",
                action="capturing_chain",
                action_params={},
                job_type="one_shot",
                scheduled_at=now,
                trigger_on_job_id=mid.job_id,
                trigger_on_status="ANY",
            )
            sink2 = Job(
                user_id="reversed-fanout-test",
                description="sink2",
                action="capturing_chain",
                action_params={},
                job_type="one_shot",
                scheduled_at=now,
                trigger_on_job_id=mid.job_id,
                trigger_on_status="ANY",
            )
            session.add(sink1)
            session.add(sink2)
            await session.flush()
            run_sink1_0 = _waiting(sink1, run_mid0.run_id)
            run_sink2_0 = _waiting(sink2, run_mid0.run_id)
            session.add(run_sink1_0)
            session.add(run_sink2_0)
            await session.flush()
            for r in (run_sink1_0, run_sink2_0):
                session.add(
                    RunEvent(
                        run_id=r.run_id,
                        job_id=r.job_id,
                        event_type="CREATED",
                        status_from=None,
                        status_to="WAITING",
                    )
                )
    return root, mid, sink1, sink2, run_root0, run_mid0, run_sink1_0, run_sink2_0


@pytest.mark.integration
async def test_fan_out_cascade_refires_under_reversed_order(session_factory, sqs):
    """root → mid → (sink1 + sink2) re-fires every tick under recurring-poll-FIRST order.

    The recurring poll runs before any flip each tick, arming the full WAITING cascade
    (mid + both sinks) while the previous tick's runs are still live. After #234 that
    arm is unconditional, so all three downstream levels re-fire per tick and each sink
    reads ITS OWN tick's mid run_id.
    """
    (
        root,
        mid,
        sink1,
        sink2,
        run_root0,
        run_mid0,
        run_sink1_0,
        run_sink2_0,
    ) = await _seed_fanout_chain(session_factory)

    mid_run_ids: list[int] = []
    sink1_run_ids: list[int] = []
    sink2_run_ids: list[int] = []

    root_current = run_root0
    mid_current_waiting = run_mid0
    sink1_current_waiting = run_sink1_0
    sink2_current_waiting = run_sink2_0

    for tick in range(3):
        # --- Step 1: complete root ---
        await _terminate_run(
            session_factory, root_current, status="SUCCEEDED", result={"tick": tick}
        )

        # --- Step 2 (REVERSED): recurring poll FIRST — arms the next-tick cascade while
        #     this tick's mid/sink WAITING runs are still live. ---
        assert await recurring_poll_once(session_factory) >= 1

        next_root = None
        next_mid_waiting = None
        if tick < 2:
            next_root = (await _runs_by_status(session_factory, root.job_id, "PENDING"))[0]
            # mid's next WAITING run is armed and points at the new root run.
            next_mid_waiting = next(
                w
                for w in await _runs_by_status(session_factory, mid.job_id, "WAITING")
                if w.run_id != mid_current_waiting.run_id
            )
            assert next_mid_waiting.wait_for_run_id == next_root.run_id

        # --- Step 3: flip mid's current WAITING → PENDING, then execute it ---
        assert await chain_poll_once(session_factory) >= 1
        mid_pending = [
            r
            for r in await _runs_by_status(session_factory, mid.job_id, "PENDING")
            if r.run_id == mid_current_waiting.run_id
        ]
        assert len(mid_pending) == 1, f"tick {tick}: mid current run not flipped to PENDING"
        await process_one(
            session_factory,
            sqs,
            _sqs_message(mid_current_waiting.run_id, mid.job_id),
            registry=_REGISTRY,
        )
        mid_run_ids.append(mid_current_waiting.run_id)

        # --- Step 4: flip both sinks' current WAITING runs, then execute them ---
        assert await chain_poll_once(session_factory) >= 1
        for sink_job, current_waiting, ids in [
            (sink1, sink1_current_waiting, sink1_run_ids),
            (sink2, sink2_current_waiting, sink2_run_ids),
        ]:
            pending = [
                r
                for r in await _runs_by_status(session_factory, sink_job.job_id, "PENDING")
                if r.run_id == current_waiting.run_id
            ]
            assert len(pending) == 1, (
                f"tick {tick}: sink {sink_job.job_id} current run not flipped to PENDING"
            )
            await process_one(
                session_factory,
                sqs,
                _sqs_message(current_waiting.run_id, sink_job.job_id),
                registry=_REGISTRY,
            )
            ids.append(current_waiting.run_id)

        if tick < 2:
            root_current = next_root
            mid_current_waiting = next_mid_waiting
            sink1_current_waiting = next(
                w
                for w in await _runs_by_status(session_factory, sink1.job_id, "WAITING")
                if w.wait_for_run_id == next_mid_waiting.run_id
            )
            sink2_current_waiting = next(
                w
                for w in await _runs_by_status(session_factory, sink2.job_id, "WAITING")
                if w.wait_for_run_id == next_mid_waiting.run_id
            )

    # --- Final: each sink ran 3 times, each reading ITS OWN tick's mid run_id ---
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

    assert len(s1) == 3, f"expected 3 SUCCEEDED sink1 runs, got {len(s1)}"
    assert len(s2) == 3, f"expected 3 SUCCEEDED sink2 runs, got {len(s2)}"
    s1_by_id = {r.run_id: json.loads(r.result) for r in s1}
    s2_by_id = {r.run_id: json.loads(r.result) for r in s2}
    for tick in range(3):
        assert s1_by_id[sink1_run_ids[tick]]["received_from_run_id"] == mid_run_ids[tick]
        assert s2_by_id[sink2_run_ids[tick]]["received_from_run_id"] == mid_run_ids[tick]
