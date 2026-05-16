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
