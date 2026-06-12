"""Integration tests for app/workers/chain_watcher.py (full flip logic, W2).

Requires running Postgres (DATABASE_URL set in environment).

Run with:
    uv run pytest -m integration tests/integration/test_chain_watcher.py
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select, text, update
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


async def _insert_chain(
    factory: async_sessionmaker,
    *,
    event_type: str = "SUCCEEDED",
    trigger_on_status: str = "SUCCEEDED",
    upstream_run_status: str | None = None,
) -> tuple[Job, Job, JobRun, JobRun, RunEvent]:
    """Insert upstream job + downstream chained job, plus a terminal RunEvent for upstream.

    Returns (upstream_job, downstream_job, upstream_run, downstream_run, terminal_event).
    upstream_run_status defaults to event_type (upstream has already terminated).
    """
    final_upstream_status = upstream_run_status if upstream_run_status is not None else event_type
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
                user_id=upstream_job.user_id,
                scheduled_at=scheduled,
                status=final_upstream_status,
                finish_at=datetime.now(tz=UTC),
            )
            session.add(upstream_run)
            await session.flush()

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
                user_id=downstream_job.user_id,
                scheduled_at=scheduled + timedelta(seconds=1),
                status="WAITING",
                wait_for_run_id=upstream_run.run_id,
            )
            session.add(downstream_run)
            await session.flush()

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
# 9-combination matrix: (event_type) × (trigger_on_status)
# ---------------------------------------------------------------------------
# trigger_on_status == event_type  → PENDING  (literal match)
# trigger_on_status == "ANY"       → PENDING  (matches everything including CANCELLED)
# otherwise                        → CANCELLED (mismatch)


@pytest.mark.integration
@pytest.mark.parametrize(
    "event_type, trigger_on_status, expected_status",
    [
        # Literal matches → PENDING
        ("SUCCEEDED", "SUCCEEDED", "PENDING"),
        ("FAILED", "FAILED", "PENDING"),
        ("CANCELLED", "CANCELLED", "PENDING"),
        # ANY → PENDING for all terminal types (including CANCELLED — by design)
        ("SUCCEEDED", "ANY", "PENDING"),
        ("FAILED", "ANY", "PENDING"),
        ("CANCELLED", "ANY", "PENDING"),
        # Mismatches → CANCELLED
        ("SUCCEEDED", "FAILED", "CANCELLED"),
        ("FAILED", "SUCCEEDED", "CANCELLED"),
        ("CANCELLED", "SUCCEEDED", "CANCELLED"),
    ],
)
async def test_all_9_combinations(session_factory, event_type, trigger_on_status, expected_status):
    """All 9 (event_type × trigger_on_status) combinations flip to the correct target.

    Specifically: CANCELLED × ANY → PENDING (not CANCELLED) — the "ANY includes
    CANCELLED" design decision from ADR-020.
    """
    _, _, _, downstream_run, event = await _insert_chain(
        session_factory,
        event_type=event_type,
        trigger_on_status=trigger_on_status,
    )

    count = await poll_once(session_factory)
    assert count == 1

    async with session_factory() as session:
        async with session.begin():
            refreshed = (
                await session.execute(select(JobRun).where(JobRun.run_id == downstream_run.run_id))
            ).scalar_one()
            refreshed_event = (
                await session.execute(select(RunEvent).where(RunEvent.event_id == event.event_id))
            ).scalar_one()

    assert refreshed.status == expected_status, (
        f"event={event_type}, trigger={trigger_on_status}: "
        f"expected {expected_status}, got {refreshed.status}"
    )
    assert PROCESSED_BY_KEY in refreshed_event.processed_by


# ---------------------------------------------------------------------------
# Idempotency: calling tick twice processes each event exactly once
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_idempotent_tick(session_factory):
    """Calling poll_once twice processes each event exactly once."""
    _, _, _, downstream_run, _ = await _insert_chain(session_factory)

    count1 = await poll_once(session_factory)
    assert count1 == 1

    count2 = await poll_once(session_factory)
    assert count2 == 0

    async with session_factory() as session:
        async with session.begin():
            refreshed = (
                await session.execute(select(JobRun).where(JobRun.run_id == downstream_run.run_id))
            ).scalar_one()

    assert refreshed.status == "PENDING"


# ---------------------------------------------------------------------------
# Recurring upstream: first terminal event flips B; subsequent events do NOT re-flip
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_recurring_upstream_flips_downstream_once(session_factory):
    """Recurring Job A → one-shot Job B: first terminal event flips B; subsequent don't."""
    scheduled = datetime.now(tz=UTC) - timedelta(hours=1)

    async with session_factory() as session:
        async with session.begin():
            # Job A: recurring (use job_type=recurring + cron_expr to satisfy DB constraint)
            job_a = Job(
                user_id="chain-recurring-test",
                description="recurring upstream",
                action="echo",
                action_params={"message": "tick"},
                job_type="recurring",
                cron_expr="* * * * *",
                timezone="UTC",
            )
            session.add(job_a)
            await session.flush()

            bucket = scheduled.replace(minute=0, second=0, microsecond=0).isoformat()
            # First run of A (terminates)
            run_a1 = JobRun(
                time_bucket=bucket,
                job_id=job_a.job_id,
                user_id=job_a.user_id,
                scheduled_at=scheduled,
                status="SUCCEEDED",
                finish_at=datetime.now(tz=UTC),
            )
            session.add(run_a1)
            await session.flush()

            # Job B: chained to A, waits for run_a1
            job_b = Job(
                user_id="chain-recurring-test",
                description="chained one-shot",
                action="echo",
                action_params={"message": "chained"},
                job_type="one_shot",
                scheduled_at=scheduled + timedelta(seconds=1),
                trigger_on_job_id=job_a.job_id,
                trigger_on_status="SUCCEEDED",
            )
            session.add(job_b)
            await session.flush()

            run_b = JobRun(
                time_bucket=bucket,
                job_id=job_b.job_id,
                user_id=job_b.user_id,
                scheduled_at=scheduled + timedelta(seconds=1),
                status="WAITING",
                wait_for_run_id=run_a1.run_id,
            )
            session.add(run_b)
            await session.flush()

            # Terminal event for A's first run
            event_a1 = RunEvent(
                run_id=run_a1.run_id,
                job_id=job_a.job_id,
                event_type="SUCCEEDED",
                status_from="RUNNING",
                status_to="SUCCEEDED",
            )
            session.add(event_a1)

    # First tick: flips B's run to PENDING
    count1 = await poll_once(session_factory)
    assert count1 == 1

    async with session_factory() as session:
        async with session.begin():
            b_run = (
                await session.execute(select(JobRun).where(JobRun.run_id == run_b.run_id))
            ).scalar_one()
    assert b_run.status == "PENDING"

    # Simulate A's second run (recurring) terminating
    scheduled2 = datetime.now(tz=UTC) - timedelta(minutes=30)
    async with session_factory() as session:
        async with session.begin():
            bucket2 = scheduled2.replace(minute=0, second=0, microsecond=0).isoformat()
            run_a2 = JobRun(
                time_bucket=bucket2,
                job_id=job_a.job_id,
                user_id=job_a.user_id,
                scheduled_at=scheduled2,
                status="SUCCEEDED",
                finish_at=datetime.now(tz=UTC),
            )
            session.add(run_a2)
            await session.flush()
            event_a2 = RunEvent(
                run_id=run_a2.run_id,
                job_id=job_a.job_id,
                event_type="SUCCEEDED",
                status_from="RUNNING",
                status_to="SUCCEEDED",
            )
            session.add(event_a2)

    # Second tick: processes A's second terminal event, but B is past WAITING → no re-flip
    count2 = await poll_once(session_factory)
    # The event is processed (cursor stamped), but B's run is NOT re-flipped
    assert count2 == 1

    async with session_factory() as session:
        async with session.begin():
            b_run_after = (
                await session.execute(select(JobRun).where(JobRun.run_id == run_b.run_id))
            ).scalar_one()
    # B is still PENDING — it was not re-flipped
    assert b_run_after.status == "PENDING"


# ---------------------------------------------------------------------------
# processed_by["chain"] stamped atomically with the flip
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_processed_by_stamped_atomically(session_factory):
    """processed_by["chain_watcher"] is set in the same transaction as the flip."""
    _, _, _, downstream_run, event = await _insert_chain(session_factory)

    count = await poll_once(session_factory)
    assert count == 1

    async with session_factory() as session:
        async with session.begin():
            refreshed_event = (
                await session.execute(select(RunEvent).where(RunEvent.event_id == event.event_id))
            ).scalar_one()
            refreshed_run = (
                await session.execute(select(JobRun).where(JobRun.run_id == downstream_run.run_id))
            ).scalar_one()

    assert PROCESSED_BY_KEY in refreshed_event.processed_by
    assert refreshed_run.status == "PENDING"


# ---------------------------------------------------------------------------
# Two cursors (recurring_watcher + chain_watcher) are independent
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_chain_watcher_two_cursors_independent(session_factory):
    """recurring_watcher and chain_watcher use separate processed_by keys."""
    *_, event = await _insert_chain(session_factory)

    # Simulate recurring_watcher having already stamped its own key
    async with session_factory() as session:
        async with session.begin():
            ev = (
                await session.execute(select(RunEvent).where(RunEvent.event_id == event.event_id))
            ).scalar_one()
            new_pb = dict(ev.processed_by)
            new_pb["recurring_watcher"] = datetime.now(tz=UTC).isoformat()
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


# ---------------------------------------------------------------------------
# Events with no downstream WAITING runs are stamped but cause no flip
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_terminal_event_without_waiting_run_is_stamped(session_factory):
    """Terminal event with no matching WAITING run is stamped and counted, not skipped."""
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
                user_id=job.user_id,
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

    # Event is processed (cursor stamped) even if there are no downstream WAITING runs
    count = await poll_once(session_factory)
    assert count == 1


# ---------------------------------------------------------------------------
# RunEvent emitted: QUEUED_BY_CHAIN on match, CANCELLED_BY_CHAIN_MISS on mismatch
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_queued_by_chain_event_emitted_on_match(session_factory):
    """A QUEUED_BY_CHAIN RunEvent is emitted when the trigger matches."""
    _, _, _, downstream_run, _ = await _insert_chain(
        session_factory, event_type="SUCCEEDED", trigger_on_status="SUCCEEDED"
    )
    await poll_once(session_factory)

    async with session_factory() as session:
        async with session.begin():
            flip_event = (
                await session.execute(
                    select(RunEvent).where(
                        RunEvent.run_id == downstream_run.run_id,
                        RunEvent.event_type == "QUEUED_BY_CHAIN",
                    )
                )
            ).scalar_one_or_none()

    assert flip_event is not None
    assert flip_event.status_from == "WAITING"
    assert flip_event.status_to == "PENDING"


@pytest.mark.integration
async def test_cancelled_by_chain_miss_event_emitted_on_mismatch(session_factory):
    """A CANCELLED_BY_CHAIN_MISS RunEvent is emitted on trigger mismatch."""
    _, _, _, downstream_run, _ = await _insert_chain(
        session_factory, event_type="FAILED", trigger_on_status="SUCCEEDED"
    )
    await poll_once(session_factory)

    async with session_factory() as session:
        async with session.begin():
            flip_event = (
                await session.execute(
                    select(RunEvent).where(
                        RunEvent.run_id == downstream_run.run_id,
                        RunEvent.event_type == "CANCELLED_BY_CHAIN_MISS",
                    )
                )
            ).scalar_one_or_none()

    assert flip_event is not None
    assert flip_event.status_from == "WAITING"
    assert flip_event.status_to == "CANCELLED"


# ---------------------------------------------------------------------------
# Legacy: does not reprocess an already-stamped event
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_chain_watcher_does_not_reprocess_stamped_event(session_factory):
    """Event already stamped with chain_watcher key → skipped on second poll."""
    *_, event = await _insert_chain(session_factory)

    count1 = await poll_once(session_factory)
    assert count1 == 1

    count2 = await poll_once(session_factory)
    assert count2 == 0


# ---------------------------------------------------------------------------
# Slow-consumer drop (ADR-065 §4, issue #227; predicate revised by #234):
# When a downstream job already has an *executing* run, flip WAITING → CANCELLED
# with CANCELLED_SLOW_CONSUMER instead of PENDING.  These tests set the
# overlapping executing run up directly to isolate the flip-time decision; the
# overlap arising through the real arm path is covered in test_chain_concurrency.py.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_slow_consumer_drop_cancels_waiting_run(session_factory):
    """Upstream outpaces downstream: WAITING run is cancelled (slow-consumer drop).

    Setup:
    - Upstream Job A (one_shot) completes → terminal RunEvent.
    - Downstream Job B (chained to A) has:
        (a) a pre-existing PENDING run (the "live" run — B is still processing),
        (b) a WAITING run armed against A's terminal run (the incoming tick).
    - ChainWatcher processes A's terminal event.
    - Expected: B's WAITING run → CANCELLED (CANCELLED_SLOW_CONSUMER event),
      NOT PENDING. The single-live-run invariant holds.
    """
    scheduled = datetime.now(tz=UTC) - timedelta(hours=1)

    async with session_factory() as session:
        async with session.begin():
            # Job A: one_shot upstream
            job_a = Job(
                user_id="slow-consumer-test",
                description="upstream job A",
                action="echo",
                action_params={"message": "upstream"},
                job_type="one_shot",
                scheduled_at=scheduled,
            )
            session.add(job_a)
            await session.flush()

            bucket = scheduled.replace(minute=0, second=0, microsecond=0).isoformat()
            run_a = JobRun(
                time_bucket=bucket,
                job_id=job_a.job_id,
                user_id=job_a.user_id,
                scheduled_at=scheduled,
                status="SUCCEEDED",
                finish_at=datetime.now(tz=UTC),
            )
            session.add(run_a)
            await session.flush()

            # Job B: trigger-driven downstream
            job_b = Job(
                user_id="slow-consumer-test",
                description="downstream job B",
                action="echo",
                action_params={"message": "downstream"},
                job_type="one_shot",
                scheduled_at=scheduled + timedelta(seconds=1),
                trigger_on_job_id=job_a.job_id,
                trigger_on_status="ANY",
            )
            session.add(job_b)
            await session.flush()

            # B's pre-existing PENDING run (the "slow" live run still processing)
            run_b_live = JobRun(
                time_bucket=bucket,
                job_id=job_b.job_id,
                user_id=job_b.user_id,
                scheduled_at=scheduled + timedelta(seconds=1),
                status="PENDING",
            )
            session.add(run_b_live)
            await session.flush()

            # B's NEW WAITING run armed against A's terminal run (the incoming tick)
            run_b_waiting = JobRun(
                time_bucket=bucket,
                job_id=job_b.job_id,
                user_id=job_b.user_id,
                scheduled_at=scheduled + timedelta(seconds=2),
                status="WAITING",
                wait_for_run_id=run_a.run_id,
            )
            session.add(run_b_waiting)
            await session.flush()

            # Terminal event for A
            terminal_event = RunEvent(
                run_id=run_a.run_id,
                job_id=job_a.job_id,
                event_type="SUCCEEDED",
                status_from="RUNNING",
                status_to="SUCCEEDED",
            )
            session.add(terminal_event)

    # ChainWatcher processes A's terminal event
    count = await poll_once(session_factory)
    assert count == 1

    async with session_factory() as session:
        async with session.begin():
            refreshed_waiting = (
                await session.execute(select(JobRun).where(JobRun.run_id == run_b_waiting.run_id))
            ).scalar_one()

            refreshed_live = (
                await session.execute(select(JobRun).where(JobRun.run_id == run_b_live.run_id))
            ).scalar_one()

            # The CANCELLED_SLOW_CONSUMER event should exist
            drop_event = (
                await session.execute(
                    select(RunEvent).where(
                        RunEvent.run_id == run_b_waiting.run_id,
                        RunEvent.event_type == "CANCELLED_SLOW_CONSUMER",
                    )
                )
            ).scalar_one_or_none()

    # The incoming tick was dropped: WAITING → CANCELLED
    assert refreshed_waiting.status == "CANCELLED", (
        f"Expected slow-consumer WAITING run to be CANCELLED, got {refreshed_waiting.status}"
    )
    # The live run is untouched (still PENDING)
    assert refreshed_live.status == "PENDING", (
        f"Live PENDING run should be unchanged, got {refreshed_live.status}"
    )
    # Auditable record: CANCELLED_SLOW_CONSUMER event emitted
    assert drop_event is not None, "Expected CANCELLED_SLOW_CONSUMER RunEvent to be emitted"
    assert drop_event.status_from == "WAITING"
    assert drop_event.status_to == "CANCELLED"


@pytest.mark.integration
async def test_slow_consumer_single_live_run_invariant(session_factory):
    """At no point do two executing runs for the same job coexist.

    After the slow-consumer drop, only one executing run exists for Job B (#234:
    the invariant is now at-most-one-*executing*-run). This is the RUNNING case —
    even stronger: a mid-execution run is protected.
    """
    scheduled = datetime.now(tz=UTC) - timedelta(hours=1)

    async with session_factory() as session:
        async with session.begin():
            job_a = Job(
                user_id="slow-consumer-invariant-test",
                description="upstream",
                action="echo",
                action_params={"message": "up"},
                job_type="one_shot",
                scheduled_at=scheduled,
            )
            session.add(job_a)
            await session.flush()

            bucket = scheduled.replace(minute=0, second=0, microsecond=0).isoformat()
            run_a = JobRun(
                time_bucket=bucket,
                job_id=job_a.job_id,
                user_id=job_a.user_id,
                scheduled_at=scheduled,
                status="SUCCEEDED",
                finish_at=datetime.now(tz=UTC),
            )
            session.add(run_a)
            await session.flush()

            job_b = Job(
                user_id="slow-consumer-invariant-test",
                description="downstream",
                action="echo",
                action_params={"message": "down"},
                job_type="one_shot",
                scheduled_at=scheduled + timedelta(seconds=1),
                trigger_on_job_id=job_a.job_id,
                trigger_on_status="SUCCEEDED",
            )
            session.add(job_b)
            await session.flush()

            # B already has a live RUNNING run (in-flight from a prior tick)
            run_b_running = JobRun(
                time_bucket=bucket,
                job_id=job_b.job_id,
                user_id=job_b.user_id,
                scheduled_at=scheduled + timedelta(seconds=1),
                status="RUNNING",
            )
            session.add(run_b_running)
            await session.flush()

            # B also has a WAITING run (the new tick — would violate single-live-run)
            run_b_waiting = JobRun(
                time_bucket=bucket,
                job_id=job_b.job_id,
                user_id=job_b.user_id,
                scheduled_at=scheduled + timedelta(seconds=2),
                status="WAITING",
                wait_for_run_id=run_a.run_id,
            )
            session.add(run_b_waiting)
            await session.flush()

            session.add(
                RunEvent(
                    run_id=run_a.run_id,
                    job_id=job_a.job_id,
                    event_type="SUCCEEDED",
                    status_from="RUNNING",
                    status_to="SUCCEEDED",
                )
            )

    await poll_once(session_factory)

    # After the drop, count *executing* runs for job B (WAITING excluded — #234)
    _EXECUTING_STATUSES = ("PENDING", "QUEUED", "RUNNING", "RETRYING")
    async with session_factory() as session:
        async with session.begin():
            live_runs = (
                (
                    await session.execute(
                        select(JobRun).where(
                            JobRun.job_id == job_b.job_id,
                            JobRun.status.in_(list(_EXECUTING_STATUSES)),
                        )
                    )
                )
                .scalars()
                .all()
            )

    # At most one executing run for Job B at any time
    assert len(live_runs) == 1, (
        f"Expected at most 1 executing run for job_b, got {len(live_runs)}: "
        f"{[(r.run_id, r.status) for r in live_runs]}"
    )
    # The surviving executing run is the RUNNING one (not the dropped WAITING)
    assert live_runs[0].run_id == run_b_running.run_id
    assert live_runs[0].status == "RUNNING"


@pytest.mark.integration
async def test_normal_flip_when_no_other_live_run(session_factory):
    """When there is no other live run, the normal trigger-status match logic applies.

    Regression test: slow-consumer check must NOT fire when the WAITING run is
    the only non-terminal run for the job.
    """
    _, _, _, downstream_run, _ = await _insert_chain(
        session_factory, event_type="SUCCEEDED", trigger_on_status="SUCCEEDED"
    )
    # Only one run exists for the downstream job (the WAITING run itself)
    count = await poll_once(session_factory)
    assert count == 1

    async with session_factory() as session:
        async with session.begin():
            refreshed = (
                await session.execute(select(JobRun).where(JobRun.run_id == downstream_run.run_id))
            ).scalar_one()

    # Normal flip: WAITING → PENDING (no slow-consumer drop)
    assert refreshed.status == "PENDING", f"Expected normal flip to PENDING, got {refreshed.status}"
