"""Unit tests for app/domain/run_materializer.py (continuation model, ADR-067).

Tests the _spawn_run primitive, materialize_initial (schedule-driven only),
materialize_successor (no arming), and materialize_downstream (trigger-driven
run created on an upstream terminal). These use mock sessions (no DB required)
to verify the domain logic in isolation.

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
    has_executing_run,
    materialize_downstream,
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


def _make_session(no_live_run=True, flushed_run_id=99):
    """Build a mock async session that supports add/flush/execute.

    TextClause statements (pg_advisory_xact_lock calls, issue #237) pass through
    without advancing the call counter. Every non-lock execute returns the
    executing-run check result, so _spawn_run's single has_executing_run query is
    answered. The continuation model no longer queries downstream jobs from the
    materializer, so the old check/downstream alternation is gone.
    """
    from sqlalchemy.sql.elements import TextClause

    session = AsyncMock()

    session._added = []
    session.add = MagicMock(side_effect=lambda obj: session._added.append(obj))

    flush_call_count = [0]

    async def _flush():
        flush_call_count[0] += 1
        for obj in session._added:
            if isinstance(obj, JobRun) and not hasattr(obj, "_flushed"):
                obj.run_id = flushed_run_id + flush_call_count[0] - 1
                obj._flushed = True

    session.flush = _flush

    scalar_result = MagicMock()
    scalar_result.scalar.return_value = not no_live_run  # scalar() False = no executing run

    async def _execute(stmt, *args, **kwargs):
        if isinstance(stmt, TextClause):
            return MagicMock()
        return scalar_result

    session.execute = _execute
    return session


# ---------------------------------------------------------------------------
# materialize_initial — schedule-driven only
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_materialize_initial_schedule_driven_creates_pending_run():
    """A schedule-driven job gets a PENDING run with no upstream dependency."""
    job = _make_job(job_id=1, cron_expr="@hourly")
    run_at = datetime.now(tz=UTC) + timedelta(hours=1)

    session = _make_session(no_live_run=True)

    run = await materialize_initial(session, job, run_at=run_at)

    assert run is not None
    job_runs = [o for o in session._added if isinstance(o, JobRun)]
    assert len(job_runs) == 1
    spawned = job_runs[0]
    assert spawned.status == "PENDING"
    assert spawned.scheduled_at == run_at
    assert spawned.job_id == job.job_id
    assert spawned.wait_for_run_id is None


@pytest.mark.asyncio
async def test_materialize_initial_concurrency_raises():
    """When a live run already exists, ConcurrencyError is raised."""
    job = _make_job(job_id=1, cron_expr="@hourly")
    run_at = datetime.now(tz=UTC) + timedelta(hours=1)

    session = _make_session(no_live_run=False)  # live run exists

    with pytest.raises(ConcurrencyError):
        await materialize_initial(session, job, run_at=run_at)


# ---------------------------------------------------------------------------
# materialize_successor — next cron occurrence, NO arming (ADR-067)
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
    assert len(job_runs) == 1
    spawned = job_runs[0]
    assert spawned.status == "PENDING"
    assert spawned.job_id == job.job_id
    assert spawned.scheduled_at > prev_run.scheduled_at


@pytest.mark.asyncio
async def test_materialize_successor_does_not_arm_downstream():
    """No WAITING downstream run is materialised: continuation replaces arming."""
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
    assert len(job_runs) == 1, "successor must create only the root run — no arming"
    assert all(r.status != "WAITING" for r in job_runs), "no WAITING run may be produced"


# ---------------------------------------------------------------------------
# materialize_downstream — trigger-driven run on an upstream terminal (ADR-067)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_materialize_downstream_creates_pending_run_with_from_run_id():
    """A downstream run is PENDING (not WAITING) and carries the upstream run_id."""
    downstream = _make_job(job_id=2, trigger_on_job_id=1, trigger_on_status="ANY")
    session = _make_session(no_live_run=True)
    occurred_at = datetime(2026, 1, 1, 10, 30, tzinfo=UTC)

    run = await materialize_downstream(
        session,
        downstream_job=downstream,
        upstream_run_id=42,
        occurred_at=occurred_at,
    )

    assert run.status == "PENDING", "continuation creates PENDING directly — no WAITING limbo"
    assert run.wait_for_run_id == 42, "wait_for_run_id carries the upstream terminal run_id"
    assert run.job_id == downstream.job_id
    assert run.scheduled_at == occurred_at


@pytest.mark.asyncio
async def test_materialize_downstream_slow_consumer_raises_concurrency():
    """If the downstream already has an executing run, ConcurrencyError signals skip."""
    downstream = _make_job(job_id=2, trigger_on_job_id=1, trigger_on_status="ANY")
    session = _make_session(no_live_run=False)  # downstream already executing

    with pytest.raises(ConcurrencyError):
        await materialize_downstream(
            session,
            downstream_job=downstream,
            upstream_run_id=42,
            occurred_at=datetime.now(tz=UTC),
        )


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


# ---------------------------------------------------------------------------
# has_executing_run — public predicate (shared by the consumer + _spawn_run)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_has_executing_run_returns_true_when_executing_run_exists():
    """has_executing_run returns True when an executing run exists for the job."""
    session = _make_session(no_live_run=False)  # executing run exists
    result = await has_executing_run(session, job_id=1)
    assert result is True


@pytest.mark.asyncio
async def test_has_executing_run_returns_false_when_no_executing_run():
    """has_executing_run returns False when no executing run exists."""
    session = _make_session(no_live_run=True)  # no executing run
    result = await has_executing_run(session, job_id=1)
    assert result is False


def _make_capturing_session(scalar_value: bool):
    """Mock session that records the statement passed to execute().

    Lets a test assert on the *compiled SQL* of the executing-run query (i.e. which
    statuses it counts) rather than only trusting a canned scalar result — the
    statement itself is the seam under test.
    """
    session = AsyncMock()
    captured: list = []
    scalar_result = MagicMock()
    scalar_result.scalar.return_value = scalar_value

    async def _execute(stmt, *args, **kwargs):
        captured.append(stmt)
        return scalar_result

    session.execute = _execute
    return session, captured


@pytest.mark.asyncio
async def test_has_executing_run_counts_only_executing_statuses_not_waiting():
    """has_executing_run's SQL counts PENDING/QUEUED/RUNNING/RETRYING but NOT WAITING.

    The executing-only predicate is the heart of #234: a legacy WAITING run must
    not be counted, so it never blocks a new run. Asserting on the compiled SQL
    guards the status set — a test that only checks the (mocked) return value would
    still pass if WAITING crept back in.
    """
    session, captured = _make_capturing_session(scalar_value=False)

    result = await has_executing_run(session, job_id=1)

    assert result is False
    assert len(captured) == 1
    sql = str(captured[0].compile(compile_kwargs={"literal_binds": True}))
    for executing in ("'PENDING'", "'QUEUED'", "'RUNNING'", "'RETRYING'"):
        assert executing in sql, f"{executing} must be counted as executing"
    assert "'WAITING'" not in sql, "WAITING must NOT count as an executing run (#234)"


@pytest.mark.asyncio
async def test_has_executing_run_builds_no_run_id_inequality():
    """has_executing_run filters by (job_id, status) only — no run_id exclusion."""
    session, captured = _make_capturing_session(scalar_value=True)

    result = await has_executing_run(session, job_id=1)

    assert result is True
    assert len(captured) == 1
    sql = str(captured[0].compile(compile_kwargs={"literal_binds": True}))
    assert "run_id !=" not in sql


# ---------------------------------------------------------------------------
# Advisory lock — per-job serialization (issue #237)
# ---------------------------------------------------------------------------
#
# _spawn_run must acquire pg_advisory_xact_lock(job_id) BEFORE the
# has_executing_run check so that concurrent sessions cannot both pass the check
# and both write (the non-locking SELECT-then-act race).


def _make_capturing_session_with_lock_tracking(scalar_value: bool):
    """Mock session that records ALL statements in order.

    The order matters for #237: pg_advisory_xact_lock must appear BEFORE the
    has_executing_run SELECT.
    """
    from sqlalchemy.sql.elements import TextClause

    session = AsyncMock()
    ordered_calls: list[str] = []  # "lock:<job_id>" or "check"

    scalar_result = MagicMock()
    scalar_result.scalar.return_value = scalar_value

    async def _execute(stmt, *args, **kwargs):
        if isinstance(stmt, TextClause):
            params = args[0] if args else kwargs.get("parameters", {}) or {}
            ordered_calls.append(f"lock:{params.get('job_id', '?')}")
            return MagicMock()
        ordered_calls.append("check")
        return scalar_result

    session.execute = _execute

    async def _flush():
        pass

    session.flush = _flush
    session._added = []
    session.add = MagicMock(side_effect=lambda obj: session._added.append(obj))

    return session, ordered_calls


@pytest.mark.asyncio
async def test_spawn_run_acquires_advisory_lock_before_concurrency_check():
    """_spawn_run must acquire pg_advisory_xact_lock(job_id) before has_executing_run.

    The lock serializes the check-then-write per job so two concurrent sessions
    cannot both pass the check and both insert an executing run (issue #237).
    """
    job = _make_job(job_id=42, cron_expr="@hourly")
    run_at = datetime.now(tz=UTC) + timedelta(hours=1)

    session, calls = _make_capturing_session_with_lock_tracking(scalar_value=False)

    await materialize_initial(session, job, run_at=run_at)

    assert calls, "no execute calls recorded"
    assert calls[0].startswith("lock:"), (
        f"first execute call must be the advisory lock, got: {calls[0]!r}"
    )
    assert "42" in calls[0], f"advisory lock must use the job's job_id (42), got: {calls[0]!r}"
    check_idx = next((i for i, c in enumerate(calls) if c == "check"), None)
    lock_idx = next((i for i, c in enumerate(calls) if c.startswith("lock:")), None)
    assert lock_idx is not None, "no advisory lock call found"
    assert check_idx is not None, "no has_executing_run check call found"
    assert lock_idx < check_idx, (
        f"advisory lock (pos {lock_idx}) must precede the executing-run check (pos {check_idx})"
    )
