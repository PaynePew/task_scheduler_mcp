"""Unit tests for app/domain/run_materializer.py.

Tests _spawn_run primitive, materialize_initial, materialize_successor, and _arm.
These use mock sessions (no DB required) to verify the domain logic in isolation.

Run with:
    uv run pytest -m "not integration" tests/unit/test_run_materializer.py
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.db.models import Job, JobRun
from app.domain.run_materializer import (
    ConcurrencyError,
    materialize_initial,
    materialize_successor,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_job(
    job_id=1,
    user_id="u1",
    cron_expr=None,
    timezone="UTC",
    trigger_on_job_id=None,
    trigger_on_status=None,
    cancelled_at=None,
):
    job = MagicMock(spec=Job)
    job.job_id = job_id
    job.user_id = user_id
    job.cron_expr = cron_expr
    job.timezone = timezone
    job.trigger_on_job_id = trigger_on_job_id
    job.trigger_on_status = trigger_on_status
    job.cancelled_at = cancelled_at
    return job


def _make_run(run_id=10, job_id=1, user_id="u1", status="SUCCEEDED", scheduled_at=None):
    run = MagicMock(spec=JobRun)
    run.run_id = run_id
    run.job_id = job_id
    run.user_id = user_id
    run.status = status
    run.scheduled_at = scheduled_at or (datetime.now(tz=UTC) - timedelta(hours=1))
    return run


def _make_session(
    no_live_run=True,
    flushed_run_id=99,
    downstream_jobs=None,
):
    """Build a mock async session that supports add/flush/execute."""
    session = AsyncMock()

    # Track objects added to the session
    session._added = []
    session.add = MagicMock(side_effect=lambda obj: session._added.append(obj))

    # Flush assigns a run_id to the most recently added JobRun
    flush_call_count = [0]

    async def _flush():
        flush_call_count[0] += 1
        for obj in session._added:
            if isinstance(obj, JobRun) and not hasattr(obj, "_flushed"):
                obj.run_id = flushed_run_id + flush_call_count[0] - 1
                obj._flushed = True

    session.flush = _flush

    # execute returns no-live-run by default (for forbid-concurrency check)
    scalar_result = MagicMock()
    scalar_result.scalar.return_value = not no_live_run  # scalar() returns False = no live run

    # For downstream jobs query
    if downstream_jobs is None:
        downstream_jobs = []

    scalars_result = MagicMock()
    scalars_result.scalars.return_value.all.return_value = downstream_jobs

    execute_call_count = [0]

    async def _execute(stmt, *args, **kwargs):
        execute_call_count[0] += 1
        # First call is always the has_live_run check (returns scalar bool)
        # Subsequent calls are downstream job queries
        if execute_call_count[0] % 2 == 1:  # odd calls = live-run check
            return scalar_result
        else:  # even calls = downstream jobs
            return scalars_result

    session.execute = _execute
    return session


# ---------------------------------------------------------------------------
# materialize_initial — schedule-driven (no trigger)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_materialize_initial_schedule_driven_creates_pending_run():
    """A schedule-driven job (no trigger) gets a PENDING run."""
    job = _make_job(job_id=1, cron_expr="@hourly")
    run_at = datetime.now(tz=UTC) + timedelta(hours=1)

    session = _make_session(no_live_run=True)

    run = await materialize_initial(session, job, run_at=run_at)

    assert run is not None
    # The run was added to the session
    job_runs = [o for o in session._added if isinstance(o, JobRun)]
    assert len(job_runs) >= 1
    spawned = job_runs[0]
    assert spawned.status == "PENDING"
    assert spawned.scheduled_at == run_at
    assert spawned.job_id == job.job_id
    assert spawned.wait_for_run_id is None


@pytest.mark.asyncio
async def test_materialize_initial_trigger_driven_creates_waiting_run():
    """A trigger-driven job (trigger_on_job_id set) gets a WAITING run armed against upstream."""
    upstream_run = _make_run(run_id=42, job_id=10, status="RUNNING")
    job = _make_job(job_id=2, trigger_on_job_id=10, trigger_on_status="SUCCEEDED")
    run_at = datetime.now(tz=UTC)

    session = _make_session(no_live_run=True)

    await materialize_initial(session, job, run_at=run_at, wait_run=upstream_run)

    job_runs = [o for o in session._added if isinstance(o, JobRun)]
    assert len(job_runs) >= 1
    spawned = job_runs[0]
    assert spawned.status == "WAITING"
    assert spawned.wait_for_run_id == upstream_run.run_id


@pytest.mark.asyncio
async def test_materialize_initial_concurrency_raises():
    """When a live run already exists, ConcurrencyError is raised."""
    job = _make_job(job_id=1, cron_expr="@hourly")
    run_at = datetime.now(tz=UTC) + timedelta(hours=1)

    session = _make_session(no_live_run=False)  # live run exists

    with pytest.raises(ConcurrencyError):
        await materialize_initial(session, job, run_at=run_at)


# ---------------------------------------------------------------------------
# materialize_successor — next cron occurrence, arms downstream
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_materialize_successor_creates_pending_run():
    """materialize_successor inserts a PENDING run at the next cron tick."""
    job = _make_job(job_id=1, cron_expr="0 * * * *", timezone="UTC")
    prev_run = _make_run(
        run_id=5,
        job_id=1,
        status="SUCCEEDED",
        scheduled_at=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
    )
    session = _make_session(no_live_run=True)
    occurred_at = prev_run.scheduled_at + timedelta(minutes=30)

    await materialize_successor(session, job, prev_run=prev_run, occurred_at=occurred_at)

    job_runs = [o for o in session._added if isinstance(o, JobRun)]
    assert len(job_runs) >= 1
    spawned = job_runs[0]
    assert spawned.status == "PENDING"
    assert spawned.job_id == job.job_id
    assert spawned.scheduled_at > prev_run.scheduled_at


@pytest.mark.asyncio
async def test_materialize_successor_arms_downstream():
    """materialize_successor creates WAITING runs for each downstream job."""
    # downstream job that triggers on job 1
    downstream_job = _make_job(job_id=2, trigger_on_job_id=1, trigger_on_status="SUCCEEDED")

    job = _make_job(job_id=1, cron_expr="0 * * * *", timezone="UTC")
    prev_run = _make_run(
        run_id=5,
        job_id=1,
        status="SUCCEEDED",
        scheduled_at=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
    )

    # Session that returns the downstream job for the arm step
    session = _make_session(no_live_run=True, flushed_run_id=99, downstream_jobs=[downstream_job])
    occurred_at = prev_run.scheduled_at + timedelta(minutes=30)

    await materialize_successor(session, job, prev_run=prev_run, occurred_at=occurred_at)

    # Should have created: 1 PENDING root run + 1 WAITING downstream run
    job_runs = [o for o in session._added if isinstance(o, JobRun)]
    assert len(job_runs) >= 2

    statuses = {r.status for r in job_runs}
    assert "PENDING" in statuses
    assert "WAITING" in statuses

    waiting = next(r for r in job_runs if r.status == "WAITING")
    assert waiting.job_id == downstream_job.job_id


@pytest.mark.asyncio
async def test_materialize_successor_no_downstream_when_none():
    """materialize_successor creates only the root run when there is no downstream."""
    job = _make_job(job_id=1, cron_expr="0 * * * *", timezone="UTC")
    prev_run = _make_run(
        run_id=5,
        job_id=1,
        status="SUCCEEDED",
        scheduled_at=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
    )
    session = _make_session(no_live_run=True, downstream_jobs=[])
    occurred_at = prev_run.scheduled_at + timedelta(minutes=30)

    await materialize_successor(session, job, prev_run=prev_run, occurred_at=occurred_at)

    job_runs = [o for o in session._added if isinstance(o, JobRun)]
    assert len(job_runs) == 1
    assert job_runs[0].status == "PENDING"


# ---------------------------------------------------------------------------
# CREATED RunEvent is always emitted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_emits_created_run_event():
    """_spawn_run emits a CREATED RunEvent in the session."""
    from app.db.models import RunEvent

    job = _make_job(job_id=1, cron_expr="@hourly")
    run_at = datetime.now(tz=UTC) + timedelta(hours=1)

    session = _make_session(no_live_run=True)

    await materialize_initial(session, job, run_at=run_at)

    events = [o for o in session._added if isinstance(o, RunEvent)]
    assert len(events) >= 1
    created_event = events[0]
    assert created_event.event_type == "CREATED"


# ---------------------------------------------------------------------------
# time_bucket is derived correctly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_derives_time_bucket():
    """time_bucket is hour-truncated ISO string of run_at."""
    job = _make_job(job_id=1, cron_expr="@hourly")
    run_at = datetime(2026, 3, 15, 14, 37, 0, tzinfo=UTC)

    session = _make_session(no_live_run=True)

    await materialize_initial(session, job, run_at=run_at)

    job_runs = [o for o in session._added if isinstance(o, JobRun)]
    assert len(job_runs) >= 1
    spawned = job_runs[0]
    expected_bucket = "2026-03-15T14:00:00+00:00"
    assert spawned.time_bucket == expected_bucket
