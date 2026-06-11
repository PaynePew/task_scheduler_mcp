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


# ---------------------------------------------------------------------------
# Cascade + fan-out unit tests (S2 / issue #226)
# ---------------------------------------------------------------------------
#
# These tests require a smarter mock session that understands the per-job
# downstream query and can answer for multi-level / multi-downstream topologies.


def _make_cascade_session(
    downstream_map: dict[int, list],
    flushed_run_id: int = 100,
    start_with_downstream: bool = False,
):
    """Build a mock session for cascade + fan-out tests.

    downstream_map: {job_id: [downstream_Job, ...]}
        Maps each job_id to the list of downstream jobs that trigger on it.
        If a job_id has no entry, it has no downstream.

    All has_live_run checks return False (no live run) so spawning always
    succeeds.  flush() auto-assigns ascending run_ids.

    start_with_downstream: set True when the first execute call comes from
        _arm (downstream query) rather than _spawn_run (live-run check).
        Use this when calling _arm() directly without a preceding _spawn_run.
    """
    session = AsyncMock()

    session._added = []
    session.add = MagicMock(side_effect=lambda obj: session._added.append(obj))

    run_id_counter = [flushed_run_id]

    async def _flush():
        for obj in session._added:
            if isinstance(obj, JobRun) and not hasattr(obj, "_flushed"):
                obj.run_id = run_id_counter[0]
                run_id_counter[0] += 1
                obj._flushed = True

    session.flush = _flush

    # State machine: True = next execute is a live-run check; False = downstream-jobs query.
    # When calling _spawn_run first, the first call is live-run check (start_with_downstream=False).
    # When calling _arm directly (skipping _spawn_run for root), first call is downstream query.
    call_is_live_run = [not start_with_downstream]

    async def _execute(stmt, *args, **kwargs):
        if call_is_live_run[0]:
            # live-run check: return False (no live run — spawning always succeeds)
            call_is_live_run[0] = False
            result = MagicMock()
            result.scalar.return_value = False
            return result
        else:
            # downstream-jobs query: look up by the most recently flushed JobRun's job_id.
            call_is_live_run[0] = True
            last_run = None
            for obj in reversed(session._added):
                if isinstance(obj, JobRun) and hasattr(obj, "_flushed"):
                    last_run = obj
                    break
            if last_run is None:
                downstreams = []
            else:
                downstreams = downstream_map.get(last_run.job_id, [])
            result = MagicMock()
            result.scalars.return_value.all.return_value = downstreams
            return result

    session.execute = _execute
    return session


@pytest.mark.asyncio
async def test_arm_fans_out_to_multiple_downstream_jobs():
    """Fan-out: a single upstream PENDING run arms WAITING runs for EVERY downstream job.

    Topology: root(job_id=1) → [sink_a(job_id=2), sink_b(job_id=3)]
    Expect 1 PENDING root run + 2 WAITING downstream runs (one per sink).
    """
    from app.domain.run_materializer import _arm

    sink_a = _make_job(job_id=2, trigger_on_job_id=1, trigger_on_status="ANY")
    sink_b = _make_job(job_id=3, trigger_on_job_id=1, trigger_on_status="ANY")

    # After we spawn the root run (job_id=1), _arm looks up downstreams of job_id=1.
    # Neither sink has downstreams.
    downstream_map = {1: [sink_a, sink_b], 2: [], 3: []}
    # start_with_downstream=True because we call _arm directly (first execute = downstream query)
    session = _make_cascade_session(downstream_map, flushed_run_id=10, start_with_downstream=True)

    # Manually spawn the root run so we can call _arm on it.
    root_run = JobRun()
    root_run.run_id = 10
    root_run.job_id = 1
    root_run.user_id = "u1"
    root_run.scheduled_at = datetime.now(tz=UTC)
    root_run.status = "PENDING"
    root_run._flushed = True  # already "flushed"
    session._added.append(root_run)

    await _arm(session, root_run, depth=0)

    waiting_runs = [o for o in session._added if isinstance(o, JobRun) and o.status == "WAITING"]
    assert len(waiting_runs) == 2, (
        f"Expected 2 WAITING downstream runs (fan-out), got {len(waiting_runs)}"
    )
    downstream_job_ids = {r.job_id for r in waiting_runs}
    assert downstream_job_ids == {2, 3}, f"Expected job_ids {{2, 3}}, got {downstream_job_ids}"
    # Each WAITING run points at the root run
    for wr in waiting_runs:
        assert wr.wait_for_run_id == root_run.run_id


@pytest.mark.asyncio
async def test_arm_cascades_two_levels():
    """Cascade: _arm recurses to arm grandchildren.

    Topology: root(1) → mid(2) → sink(3)
    Arming root(1) should create WAITING mid(2) AND WAITING sink(3).
    """
    from app.domain.run_materializer import _arm

    root_run = JobRun()
    root_run.run_id = 10
    root_run.job_id = 1
    root_run.user_id = "u1"
    root_run.scheduled_at = datetime.now(tz=UTC)
    root_run.status = "PENDING"
    root_run._flushed = True

    mid_job = _make_job(job_id=2, trigger_on_job_id=1, trigger_on_status="ANY")
    sink_job = _make_job(job_id=3, trigger_on_job_id=2, trigger_on_status="ANY")

    # job 1 → [mid(2)]; job 2 → [sink(3)]; job 3 → []
    downstream_map = {1: [mid_job], 2: [sink_job], 3: []}
    # start_with_downstream=True because we call _arm directly (first execute = downstream query)
    session = _make_cascade_session(downstream_map, flushed_run_id=11, start_with_downstream=True)
    session._added.append(root_run)

    await _arm(session, root_run, depth=0)

    waiting_runs = [o for o in session._added if isinstance(o, JobRun) and o.status == "WAITING"]
    assert len(waiting_runs) == 2, f"Expected 2 WAITING runs (mid + sink), got {len(waiting_runs)}"

    # mid WAITING run points at root
    mid_run = next(r for r in waiting_runs if r.job_id == 2)
    assert mid_run.wait_for_run_id == root_run.run_id

    # sink WAITING run points at mid (NOT at root — cascade, not fan-out)
    sink_run = next(r for r in waiting_runs if r.job_id == 3)
    assert sink_run.wait_for_run_id == mid_run.run_id, (
        f"sink's wait_for_run_id={sink_run.wait_for_run_id} should point at mid"
        f" (run_id={mid_run.run_id}), not root (run_id={root_run.run_id})"
    )


@pytest.mark.asyncio
async def test_arm_respects_max_chain_depth():
    """_arm stops recursing at MAX_CHAIN_DEPTH.

    Build a linear chain long enough to exceed MAX_CHAIN_DEPTH.  At the depth
    limit the recursion must stop rather than producing infinitely nested WAITING
    runs.  The assertion: exactly MAX_CHAIN_DEPTH WAITING runs are created (one
    per level), and the deepest job's downstream is NOT armed.
    """
    from app.domain.chain_validation import MAX_CHAIN_DEPTH
    from app.domain.run_materializer import _arm

    # Build a chain of MAX_CHAIN_DEPTH + 1 jobs (so the last level exceeds depth).
    # Job i triggers on job i-1.  job_ids: 0 (root already done), 1..MAX_CHAIN_DEPTH+1.
    n = MAX_CHAIN_DEPTH + 1
    jobs = [
        _make_job(job_id=i, trigger_on_job_id=i - 1, trigger_on_status="ANY")
        for i in range(1, n + 1)
    ]

    # downstream_map: job i-1 → [job i] for i in 1..n; last job has no downstream.
    # root is job_id=0 (the run we pass to _arm directly).
    downstream_map: dict[int, list] = {}
    for i in range(n):
        downstream_map[i] = [jobs[i]] if i < n else []
    downstream_map[n] = []

    root_run = JobRun()
    root_run.run_id = 1
    root_run.job_id = 0
    root_run.user_id = "u1"
    root_run.scheduled_at = datetime.now(tz=UTC)
    root_run.status = "PENDING"
    root_run._flushed = True

    # start_with_downstream=True because we call _arm directly (first execute = downstream query)
    session = _make_cascade_session(downstream_map, flushed_run_id=2, start_with_downstream=True)
    session._added.append(root_run)

    await _arm(session, root_run, depth=0)

    waiting_runs = [o for o in session._added if isinstance(o, JobRun) and o.status == "WAITING"]
    # depth 0 arms level 1; depth 1 arms level 2; ... depth MAX_CHAIN_DEPTH-1 arms level MAX.
    # At depth == MAX_CHAIN_DEPTH, _arm returns early → level MAX+1 is NOT armed.
    assert len(waiting_runs) == MAX_CHAIN_DEPTH, (
        f"Expected exactly {MAX_CHAIN_DEPTH} WAITING runs (depth bound), got {len(waiting_runs)}"
    )


@pytest.mark.asyncio
async def test_materialize_successor_full_fanout_shape():
    """materialize_successor arms the full 3-level fan-out: root→mid→(sink1+sink2).

    Simulates the digest topology:
      root (job_id=1, cron) → mid (job_id=2) → sink_slack (job_id=3) + sink_email (job_id=4)

    After materialize_successor on root:
      - 1 PENDING root run
      - 1 WAITING mid run (pointing at root)
      - 1 WAITING sink_slack run (pointing at mid)
      - 1 WAITING sink_email run (pointing at mid)
    Total: 4 runs.
    """
    root_job = _make_job(job_id=1, cron_expr="@daily", timezone="UTC")
    mid_job = _make_job(job_id=2, trigger_on_job_id=1, trigger_on_status="ANY")
    sink_slack = _make_job(job_id=3, trigger_on_job_id=2, trigger_on_status="ANY")
    sink_email = _make_job(job_id=4, trigger_on_job_id=2, trigger_on_status="ANY")

    downstream_map = {
        1: [mid_job],
        2: [sink_slack, sink_email],
        3: [],
        4: [],
    }

    prev_run = _make_run(
        run_id=5,
        job_id=1,
        status="SUCCEEDED",
        scheduled_at=datetime(2026, 6, 10, 9, 0, tzinfo=UTC),
    )
    session = _make_cascade_session(downstream_map, flushed_run_id=10)
    occurred_at = prev_run.scheduled_at + timedelta(minutes=30)

    await materialize_successor(session, root_job, prev_run=prev_run, occurred_at=occurred_at)

    job_runs = [o for o in session._added if isinstance(o, JobRun)]
    assert len(job_runs) == 4, (
        f"Expected 4 runs (root + mid + slack + email), got {len(job_runs)}: "
        f"{[(r.job_id, r.status) for r in job_runs]}"
    )

    root_runs = [r for r in job_runs if r.status == "PENDING"]
    assert len(root_runs) == 1
    root_run = root_runs[0]

    mid_runs = [r for r in job_runs if r.job_id == 2]
    assert len(mid_runs) == 1
    mid_run = mid_runs[0]
    assert mid_run.status == "WAITING"
    assert mid_run.wait_for_run_id == root_run.run_id

    sink_runs = [r for r in job_runs if r.job_id in (3, 4)]
    assert len(sink_runs) == 2
    for sr in sink_runs:
        assert sr.status == "WAITING"
        assert sr.wait_for_run_id == mid_run.run_id, (
            f"sink job_id={sr.job_id} should point at mid (run_id={mid_run.run_id}), "
            f"got wait_for_run_id={sr.wait_for_run_id}"
        )
