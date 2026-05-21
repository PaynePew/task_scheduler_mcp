"""Integration tests for app/workers/recurring_watcher.py.

Requires running Postgres (DATABASE_URL set in environment).

Run with:
    uv run pytest -m integration tests/integration/test_recurring_watcher.py
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config.cron import next_after
from app.db.engine import create_async_engine
from app.db.models import Job, JobRun, RunEvent
from app.workers.recurring_watcher import PROCESSED_BY_KEY, poll_once

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


async def _insert_recurring_job_with_terminal_event(
    factory: async_sessionmaker,
    *,
    cron_expr: str = "0 * * * *",
    event_type: str = "SUCCEEDED",
    timezone: str = "UTC",
    cancelled_at: datetime | None = None,
) -> tuple[Job, JobRun, RunEvent]:
    """Insert a recurring Job + JobRun + terminal RunEvent, committed."""
    scheduled = datetime.now(tz=UTC) - timedelta(hours=1)
    async with factory() as session:
        async with session.begin():
            job = Job(
                user_id="recurring-watcher-test",
                description="recurring echo",
                action="echo",
                action_params={"message": "hi"},
                job_type="recurring",
                scheduled_at=None,
                cron_expr=cron_expr,
                timezone=timezone,
                cancelled_at=cancelled_at,
            )
            session.add(job)
            await session.flush()

            bucket = scheduled.replace(minute=0, second=0, microsecond=0).isoformat()
            run = JobRun(
                time_bucket=bucket,
                job_id=job.job_id,
                user_id=job.user_id,
                scheduled_at=scheduled,
                status=event_type,
                finish_at=datetime.now(tz=UTC),
            )
            session.add(run)
            await session.flush()

            event = RunEvent(
                run_id=run.run_id,
                job_id=job.job_id,
                event_type=event_type,
                status_from="RUNNING",
                status_to=event_type,
                occurred_at=scheduled + timedelta(minutes=30),
            )
            session.add(event)
    return job, run, event


# ---------------------------------------------------------------------------
# Core spawn behaviour
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_tick_spawns_exactly_one_next_run(session_factory):
    """Terminal event for active recurring job → exactly one PENDING JobRun spawned."""
    job, run, event = await _insert_recurring_job_with_terminal_event(
        session_factory,
        cron_expr="0 * * * *",  # every hour
        timezone="UTC",
    )

    count = await poll_once(session_factory)
    assert count == 1

    async with session_factory() as session:
        async with session.begin():
            all_runs = (
                (await session.execute(select(JobRun).where(JobRun.job_id == job.job_id)))
                .scalars()
                .all()
            )
            refreshed_event = (
                await session.execute(select(RunEvent).where(RunEvent.event_id == event.event_id))
            ).scalar_one()

    # Exactly one NEW run was inserted (plus the original one = 2 total)
    assert len(all_runs) == 2, f"Expected 2 runs, got {len(all_runs)}"
    new_run = next(r for r in all_runs if r.run_id != run.run_id)
    assert new_run.status == "PENDING"

    # scheduled_at must be strictly after the terminal event's occurred_at
    assert new_run.scheduled_at > event.occurred_at

    # processed_by must be stamped
    assert PROCESSED_BY_KEY in refreshed_event.processed_by


@pytest.mark.integration
async def test_tick_scheduled_at_matches_cron_next(session_factory):
    """Spawned JobRun.scheduled_at equals next_after(cron_expr, tz, occurred_at)."""
    from zoneinfo import ZoneInfo

    from app.config.cron import next_after

    occurred_at = datetime(2026, 1, 15, 8, 30, 0, tzinfo=UTC)  # fixed time for determinism
    async with session_factory() as session:
        async with session.begin():
            job = Job(
                user_id="recurring-watcher-test",
                description="recurring echo",
                action="echo",
                action_params={"message": "hi"},
                job_type="recurring",
                scheduled_at=None,
                cron_expr="0 9 * * *",  # daily at 9 AM UTC
                timezone="UTC",
            )
            session.add(job)
            await session.flush()
            bucket = occurred_at.replace(minute=0, second=0, microsecond=0).isoformat()
            run = JobRun(
                time_bucket=bucket,
                job_id=job.job_id,
                user_id=job.user_id,
                scheduled_at=occurred_at,
                status="SUCCEEDED",
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
                    occurred_at=occurred_at,
                )
            )

    await poll_once(session_factory)

    async with session_factory() as session:
        async with session.begin():
            all_runs = (
                (await session.execute(select(JobRun).where(JobRun.job_id == job.job_id)))
                .scalars()
                .all()
            )

    new_run = next(r for r in all_runs if r.run_id != run.run_id)
    expected_at = next_after("0 9 * * *", ZoneInfo("UTC"), occurred_at)
    assert new_run.scheduled_at == expected_at, (
        f"scheduled_at {new_run.scheduled_at} != expected {expected_at}"
    )


# ---------------------------------------------------------------------------
# Cancelled job: no spawn
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_cancelled_job_does_not_spawn_next_run(session_factory):
    """Job with cancelled_at set → terminal event → tick → NO new JobRun spawned."""
    job, run, event = await _insert_recurring_job_with_terminal_event(
        session_factory,
        cron_expr="0 * * * *",
        cancelled_at=datetime.now(tz=UTC),
    )

    count = await poll_once(session_factory)
    assert count == 1  # event was processed (stamped)

    async with session_factory() as session:
        async with session.begin():
            all_runs = (
                (await session.execute(select(JobRun).where(JobRun.job_id == job.job_id)))
                .scalars()
                .all()
            )
            refreshed_event = (
                await session.execute(select(RunEvent).where(RunEvent.event_id == event.event_id))
            ).scalar_one()

    # No new run was inserted
    assert len(all_runs) == 1, "Cancelled job must not spawn a next run"
    # Event was still stamped
    assert PROCESSED_BY_KEY in refreshed_event.processed_by


# ---------------------------------------------------------------------------
# Idempotency: calling tick twice processes each event exactly once
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_idempotent_tick_processes_each_event_once(session_factory):
    """Event already stamped with processed_by key → skipped on second poll."""
    job, run, event = await _insert_recurring_job_with_terminal_event(session_factory)

    # First tick → processes 1 event, inserts 1 run
    count1 = await poll_once(session_factory)
    assert count1 == 1

    # Second tick → same event is stamped, must return 0 and not insert another run
    count2 = await poll_once(session_factory)
    assert count2 == 0

    async with session_factory() as session:
        async with session.begin():
            all_runs = (
                (await session.execute(select(JobRun).where(JobRun.job_id == job.job_id)))
                .scalars()
                .all()
            )

    assert len(all_runs) == 2, "Two ticks must not produce more than 2 runs (1 original + 1 next)"


# ---------------------------------------------------------------------------
# Legacy filter tests (still valid)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_recurring_watcher_does_not_reprocess_stamped_event(session_factory):
    """Event already stamped with recurring_watcher key → skipped on second poll."""
    _, run, event = await _insert_recurring_job_with_terminal_event(session_factory)

    count1 = await poll_once(session_factory)
    assert count1 == 1

    count2 = await poll_once(session_factory)
    assert count2 == 0


@pytest.mark.integration
async def test_recurring_watcher_ignores_one_shot_jobs(session_factory):
    """Terminal event for a one_shot job (no cron_expr) → ignored."""
    scheduled = datetime.now(tz=UTC) - timedelta(hours=1)
    async with session_factory() as session:
        async with session.begin():
            job = Job(
                user_id="recurring-watcher-test",
                description="one_shot job",
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

    count = await poll_once(session_factory)
    assert count == 0


@pytest.mark.integration
async def test_recurring_watcher_processes_failed_and_cancelled_events(session_factory, caplog):
    """FAILED and CANCELLED terminal events for non-cancelled recurring jobs are processed."""
    _, _, failed_event = await _insert_recurring_job_with_terminal_event(
        session_factory, cron_expr="*/5 * * * *", event_type="FAILED"
    )
    _, _, cancelled_event = await _insert_recurring_job_with_terminal_event(
        session_factory, cron_expr="*/10 * * * *", event_type="CANCELLED"
    )

    with caplog.at_level(logging.INFO, logger="app.workers.recurring_watcher"):
        count = await poll_once(session_factory, batch_size=10)

    assert count == 2

    async with session_factory() as session:
        async with session.begin():
            for ev_id in (failed_event.event_id, cancelled_event.event_id):
                refreshed = (
                    await session.execute(select(RunEvent).where(RunEvent.event_id == ev_id))
                ).scalar_one()
                assert PROCESSED_BY_KEY in refreshed.processed_by


# ---------------------------------------------------------------------------
# Forbid-concurrency: no duplicate spawn when two events share a cron tick
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_no_duplicate_spawn_for_same_cron_tick(session_factory, caplog):
    """Two terminal events whose occurred_at fall in the same cron tick window
    must produce exactly ONE new PENDING run, not two.

    Simulates the bug scenario (issue #53): two completed runs (R1 and R2) both
    have terminal events with occurred_at values in the same minute.  For
    ``* * * * *``, next_after() maps both to the SAME scheduled_at, so without
    the pre-check a second duplicate run would be spawned.

    The pre-check in poll_once detects the PENDING run inserted for the first
    event (visible after flush, within the same transaction) and skips the
    second spawn.
    """
    # Two occurred_at values within the same minute → same next_after result.
    tick_base = datetime(2026, 1, 1, 12, 58, 0, tzinfo=UTC)
    occurred_at_1 = tick_base + timedelta(seconds=3)  # 12:58:03 → next = 12:59:00
    occurred_at_2 = tick_base + timedelta(seconds=59)  # 12:58:59 → next = 12:59:00 (same tick)

    async with session_factory() as session:
        async with session.begin():
            job = Job(
                user_id="recurring-watcher-test",
                description="every-minute echo",
                action="echo",
                action_params={"message": "tick"},
                job_type="recurring",
                scheduled_at=None,
                cron_expr="* * * * *",
                timezone="UTC",
            )
            session.add(job)
            await session.flush()

            # R1 — completed at occurred_at_1 (represents an "early" run)
            bucket1 = occurred_at_1.replace(minute=0, second=0, microsecond=0).isoformat()
            run1 = JobRun(
                time_bucket=bucket1,
                job_id=job.job_id,
                user_id=job.user_id,
                scheduled_at=tick_base - timedelta(minutes=1),  # previous tick
                status="SUCCEEDED",
                finish_at=occurred_at_1,
            )
            session.add(run1)
            await session.flush()

            session.add(
                RunEvent(
                    run_id=run1.run_id,
                    job_id=job.job_id,
                    event_type="SUCCEEDED",
                    status_from="RUNNING",
                    status_to="SUCCEEDED",
                    occurred_at=occurred_at_1,
                )
            )

            # R2 — completed at occurred_at_2 (simulates run completing before
            # its scheduled_at due to the Watcher's 5-minute lookahead window)
            bucket2 = occurred_at_2.replace(minute=0, second=0, microsecond=0).isoformat()
            run2 = JobRun(
                time_bucket=bucket2,
                job_id=job.job_id,
                user_id=job.user_id,
                scheduled_at=tick_base,  # current tick, but completed early
                status="SUCCEEDED",
                finish_at=occurred_at_2,
            )
            session.add(run2)
            await session.flush()

            session.add(
                RunEvent(
                    run_id=run2.run_id,
                    job_id=job.job_id,
                    event_type="SUCCEEDED",
                    status_from="RUNNING",
                    status_to="SUCCEEDED",
                    occurred_at=occurred_at_2,
                )
            )

    # poll_once should process both events (count == 2) but only create ONE run.
    with caplog.at_level(logging.INFO, logger="app.workers.recurring_watcher"):
        count = await poll_once(session_factory, batch_size=10)

    assert count == 2, f"Expected 2 events processed, got {count}"

    async with session_factory() as session:
        async with session.begin():
            all_runs = (
                (await session.execute(select(JobRun).where(JobRun.job_id == job.job_id)))
                .scalars()
                .all()
            )

    # 2 original runs + exactly 1 new PENDING run (not 2).
    assert len(all_runs) == 3, (
        f"Expected 3 runs (2 original + 1 new PENDING), got {len(all_runs)}: "
        f"{[(r.run_id, r.scheduled_at, r.status) for r in all_runs]}"
    )

    new_runs = [r for r in all_runs if r.status == "PENDING"]
    assert len(new_runs) == 1, f"Expected exactly 1 new PENDING run, got {len(new_runs)}"

    # The spawned run should be at the next cron tick (12:59:00).
    expected_at = next_after("* * * * *", ZoneInfo("UTC"), occurred_at_1)
    assert new_runs[0].scheduled_at == expected_at, (
        f"Spawned run at {new_runs[0].scheduled_at}, expected {expected_at}"
    )

    # The skip should be logged.
    assert any("skipping spawn" in r.message for r in caplog.records), (
        "Expected 'skipping spawn' log message for the duplicate event"
    )


# ---------------------------------------------------------------------------
# Issue #82 regression: run finishes BEFORE its own scheduled_at, in its own poll.
#
# Field-observed scenario from scheduler.paynepew.dev:
#   - Watcher's lookahead claims a PENDING run a few hundred ms before scheduled_at
#   - The echo action is < 10 ms, so the run finishes BEFORE its tick boundary
#   - SUCCEEDED event's occurred_at < prev_run.scheduled_at
#   - next_after(occurred_at) re-computes the SAME scheduled_at as the just-completed run
#   - already_live pre-check passes (prev_run is already SUCCEEDED, not in NON_TERMINAL list)
#   - Duplicate run spawned at the same scheduled_at
#
# The same-batch test above can't catch this because each event is processed in
# its OWN poll_once tick — already_live has no concurrent non-terminal run to see.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_no_duplicate_spawn_when_run_finishes_before_scheduled_at(session_factory):
    """Run completes BEFORE its scheduled_at → next spawn must not collide on that tick.

    Per-minute cron. R1.scheduled_at = T (a tick boundary), but R1 actually
    finished at T-0.1s (claimed early by Watcher's lookahead window, action ran
    in < 10ms). SUCCEEDED occurred_at = T-0.1s < scheduled_at, so the naive
    next_after(occurred_at) returns T (the same tick R1 was scheduled for).

    Fixed by anchoring next_after at max(occurred_at, scheduled_at + 1µs).
    """
    tick = datetime(2026, 1, 1, 12, 32, 0, tzinfo=UTC)
    occurred_at = tick - timedelta(milliseconds=100)  # finished 0.1s BEFORE scheduled tick

    async with session_factory() as session:
        async with session.begin():
            job = Job(
                user_id="recurring-watcher-test",
                description="every-minute echo",
                action="echo",
                action_params={"message": "tick"},
                job_type="recurring",
                scheduled_at=None,
                cron_expr="* * * * *",
                timezone="UTC",
            )
            session.add(job)
            await session.flush()

            bucket = tick.replace(minute=0, second=0, microsecond=0).isoformat()
            run1 = JobRun(
                time_bucket=bucket,
                job_id=job.job_id,
                user_id=job.user_id,
                scheduled_at=tick,
                status="SUCCEEDED",
                start_at=occurred_at - timedelta(milliseconds=5),
                finish_at=occurred_at,
            )
            session.add(run1)
            await session.flush()

            session.add(
                RunEvent(
                    run_id=run1.run_id,
                    job_id=job.job_id,
                    event_type="SUCCEEDED",
                    status_from="RUNNING",
                    status_to="SUCCEEDED",
                    occurred_at=occurred_at,
                )
            )

    count = await poll_once(session_factory)
    assert count == 1

    async with session_factory() as session:
        async with session.begin():
            all_runs = (
                (await session.execute(select(JobRun).where(JobRun.job_id == job.job_id)))
                .scalars()
                .all()
            )

    pending = [r for r in all_runs if r.status == "PENDING"]
    assert len(pending) == 1, (
        f"Expected 1 PENDING run after early-completion tick, got {len(pending)}: "
        f"{[(r.run_id, r.scheduled_at, r.status) for r in all_runs]}"
    )

    # The new run MUST be at a STRICTLY LATER tick than R1, not the same boundary.
    assert pending[0].scheduled_at > run1.scheduled_at, (
        f"Spawned run at {pending[0].scheduled_at} is not strictly after "
        f"R1.scheduled_at={run1.scheduled_at} — duplicate-tick bug"
    )
    # Specifically: the next minute boundary.
    assert pending[0].scheduled_at == tick + timedelta(minutes=1)


@pytest.mark.integration
async def test_at_most_one_run_per_scheduled_at_over_n_ticks(session_factory):
    """Drive N sequential ticks (each finishing before scheduled_at) — no two
    runs share the same scheduled_at.

    Simulates a sustained early-completion regime (fast action + Watcher
    lookahead) over multiple iterations. With the bug, this produces 2N runs
    instead of N+1.
    """
    cron = "* * * * *"
    base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    n_ticks = 5

    async with session_factory() as session:
        async with session.begin():
            job = Job(
                user_id="recurring-watcher-test",
                description="every-minute echo",
                action="echo",
                action_params={"message": "tick"},
                job_type="recurring",
                scheduled_at=None,
                cron_expr=cron,
                timezone="UTC",
            )
            session.add(job)
            await session.flush()
            job_id = job.job_id

            bucket = base.replace(minute=0, second=0, microsecond=0).isoformat()
            seed_run = JobRun(
                time_bucket=bucket,
                job_id=job_id,
                user_id=job.user_id,
                scheduled_at=base,
                status="SUCCEEDED",
                start_at=base - timedelta(milliseconds=200),
                finish_at=base - timedelta(milliseconds=100),
            )
            session.add(seed_run)
            await session.flush()
            session.add(
                RunEvent(
                    run_id=seed_run.run_id,
                    job_id=job_id,
                    event_type="SUCCEEDED",
                    status_from="RUNNING",
                    status_to="SUCCEEDED",
                    occurred_at=base - timedelta(milliseconds=100),
                )
            )

    # Drive N ticks: each poll spawns one PENDING run; we then mark it SUCCEEDED
    # at finish_at = scheduled_at - 100ms (the bug condition) and loop.
    for _ in range(n_ticks):
        await poll_once(session_factory)
        async with session_factory() as session:
            async with session.begin():
                pending_run = (
                    await session.execute(
                        select(JobRun).where(JobRun.job_id == job_id, JobRun.status == "PENDING")
                    )
                ).scalar_one()
                finish_at = pending_run.scheduled_at - timedelta(milliseconds=100)
                pending_run.status = "SUCCEEDED"
                pending_run.start_at = finish_at - timedelta(milliseconds=5)
                pending_run.finish_at = finish_at
                session.add(
                    RunEvent(
                        run_id=pending_run.run_id,
                        job_id=job_id,
                        event_type="SUCCEEDED",
                        status_from="RUNNING",
                        status_to="SUCCEEDED",
                        occurred_at=finish_at,
                    )
                )

    async with session_factory() as session:
        async with session.begin():
            all_runs = (
                (await session.execute(select(JobRun).where(JobRun.job_id == job_id)))
                .scalars()
                .all()
            )

    scheduled_ats = [r.scheduled_at for r in all_runs]
    assert len(scheduled_ats) == len(set(scheduled_ats)), (
        f"Duplicate scheduled_at across runs: "
        f"{sorted([(r.run_id, r.scheduled_at, r.status) for r in all_runs], key=lambda x: x[1])}"
    )
    # seed + N driven ticks
    assert len(all_runs) == n_ticks + 1
