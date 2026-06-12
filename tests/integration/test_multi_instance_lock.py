"""Integration tests: per-job advisory lock serializes spawn and flip (issue #237).

Two watcher instances processing the same job concurrently must not both create
an executing run.  The pre-#237 ``has_executing_run`` was a non-locking
SELECT-then-act: two sessions could both pass the check simultaneously and both
insert, violating the at-most-one-executing-run invariant (ADR-065 §4).

``pg_advisory_xact_lock(job_id)`` serializes the check-then-write per job so
the second session blocks until the first commits, then re-evaluates the check
and correctly skips (ConcurrencyError).

Tests here use real Postgres (two independent engine connections) and
``asyncio.gather`` to drive two concurrent calls.

Run with:
    uv run pytest -m integration tests/integration/test_multi_instance_lock.py
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.engine import create_async_engine
from app.db.models import Job, JobRun, RunEvent
from app.domain.run_materializer import ConcurrencyError, materialize_successor
from app.workers.chain_watcher import poll_once as chain_poll_once
from app.workers.recurring_watcher import poll_once as recurring_poll_once

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_EXECUTING = frozenset({"PENDING", "QUEUED", "RUNNING", "RETRYING"})


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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_recurring_job_with_terminated_run(
    factory: async_sessionmaker,
) -> tuple[Job, JobRun]:
    """Insert a recurring job whose tick-0 run is already SUCCEEDED.

    Returns (job, terminated_run_0) so callers can drive materialize_successor.
    """
    now = datetime.now(tz=UTC)
    bucket = now.replace(minute=0, second=0, microsecond=0).isoformat()
    async with factory() as session:
        async with session.begin():
            job = Job(
                user_id="lock-test",
                description="recurring root",
                action="echo",
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
                status="SUCCEEDED",
                finish_at=now,
            )
            session.add(run0)
            await session.flush()
            session.add(
                RunEvent(
                    run_id=run0.run_id,
                    job_id=job.job_id,
                    event_type="SUCCEEDED",
                    status_from="RUNNING",
                    status_to="SUCCEEDED",
                    occurred_at=now,
                )
            )
    return job, run0


async def _count_executing_runs(factory: async_sessionmaker, job_id: int) -> int:
    async with factory() as session:
        async with session.begin():
            result = (
                (
                    await session.execute(
                        select(JobRun).where(
                            JobRun.job_id == job_id,
                            JobRun.status.in_(list(_EXECUTING)),
                        )
                    )
                )
                .scalars()
                .all()
            )
    return len(result)


# ---------------------------------------------------------------------------
# R4-AC1: Two concurrent spawn attempts produce at most one executing run.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_concurrent_spawn_attempts_produce_at_most_one_executing_run(
    session_factory,
):
    """Two concurrent materialize_successor calls for the same job cannot both
    create an executing run (issue #237, AC1).

    The advisory lock (pg_advisory_xact_lock) serializes the check-then-write:
    one session acquires the lock and inserts the run; the second blocks until
    the first commits, then re-checks and raises ConcurrencyError.

    After both calls complete (one succeeds, one raises ConcurrencyError or is
    rolled back), exactly one PENDING run must exist for the job.
    """
    job, run0 = await _seed_recurring_job_with_terminated_run(session_factory)
    occurred_at = datetime.now(tz=UTC)

    outcomes: list[str] = []

    async def _try_spawn() -> None:
        """One concurrent spawn attempt in its own session."""
        try:
            async with session_factory() as session:
                async with session.begin():
                    await materialize_successor(
                        session,
                        job,
                        prev_run=run0,
                        occurred_at=occurred_at,
                    )
            outcomes.append("ok")
        except ConcurrencyError:
            outcomes.append("skipped")
        except Exception as exc:
            outcomes.append(f"error:{exc}")

    # Fire two concurrent spawn attempts.
    await asyncio.gather(_try_spawn(), _try_spawn())

    # Exactly one of the two must have succeeded; the other must have been skipped.
    assert outcomes.count("ok") == 1, f"expected exactly 1 spawn success, got outcomes={outcomes}"
    assert outcomes.count("skipped") == 1, (
        f"expected exactly 1 ConcurrencyError skip, got outcomes={outcomes}"
    )

    # The invariant: at most one executing run per job.
    executing = await _count_executing_runs(session_factory, job.job_id)
    assert executing == 1, (
        f"at-most-one-executing-run invariant violated: {executing} executing runs"
    )


# ---------------------------------------------------------------------------
# R4-AC2: Concurrent spawn + flip cannot produce two executing runs.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_concurrent_spawn_and_flip_produce_at_most_one_executing_run(
    session_factory,
):
    """A concurrent spawn + flip for the same downstream job cannot produce two
    executing runs (issue #237, AC2).

    Scenario:
      - A root job has terminated (tick 0 SUCCEEDED).
      - The downstream job has a WAITING run from tick 0.
      - Concurrently: RecurringJobWatcher spawns tick 1 (arming tick 1's WAITING run),
        AND ChainWatcher flips tick 0's WAITING run → PENDING.
      - The flip path locks the downstream job before the slow-consumer check, so
        it cannot race with the spawn path that is simultaneously checking/inserting.

    After both complete, the downstream job must have at most one executing run.
    """
    now = datetime.now(tz=UTC)
    bucket = now.replace(minute=0, second=0, microsecond=0).isoformat()

    async with session_factory() as session:
        async with session.begin():
            root = Job(
                user_id="spawn-flip-lock-test",
                description="root recurring",
                action="echo",
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
                status="SUCCEEDED",
                finish_at=now,
            )
            session.add(run_root0)
            await session.flush()
            session.add(
                RunEvent(
                    run_id=run_root0.run_id,
                    job_id=root.job_id,
                    event_type="SUCCEEDED",
                    status_from="RUNNING",
                    status_to="SUCCEEDED",
                    occurred_at=now,
                )
            )

            downstream = Job(
                user_id="spawn-flip-lock-test",
                description="downstream trigger-driven",
                action="echo",
                action_params={},
                job_type="one_shot",
                scheduled_at=now,
                trigger_on_job_id=root.job_id,
                trigger_on_status="ANY",
            )
            session.add(downstream)
            await session.flush()

            run_down0 = JobRun(
                time_bucket=bucket,
                job_id=downstream.job_id,
                user_id=downstream.user_id,
                scheduled_at=now,
                status="WAITING",
                wait_for_run_id=run_root0.run_id,
            )
            session.add(run_down0)
            await session.flush()
            session.add(
                RunEvent(
                    run_id=run_down0.run_id,
                    job_id=downstream.job_id,
                    event_type="CREATED",
                    status_from=None,
                    status_to="WAITING",
                )
            )

    # Concurrently: spawn (arms tick 1 WAITING + root PENDING) and flip (WAITING→PENDING
    # for tick 0's downstream WAITING run, using the ChainWatcher poll).
    await asyncio.gather(
        recurring_poll_once(session_factory),
        chain_poll_once(session_factory),
    )

    # At most one executing run per downstream job.
    executing = await _count_executing_runs(session_factory, downstream.job_id)
    assert executing <= 1, (
        f"at-most-one-executing-run invariant violated for downstream: {executing} executing runs"
    )


# ---------------------------------------------------------------------------
# R4-AC3: Watcher FOR UPDATE SKIP LOCKED claim path is unchanged.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_watcher_claim_path_unchanged(session_factory):
    """The Watcher's FOR UPDATE SKIP LOCKED claim path is not affected by the
    advisory locks (issue #237, AC3).

    The Watcher locks job_runs rows at the row level (FOR UPDATE SKIP LOCKED),
    while the advisory locks are per-job_id at the pg_advisory_xact_lock level.
    These two lock mechanisms are independent; the Watcher claim path must still
    work exactly as before.

    This test simply verifies that a PENDING run is claimed successfully while
    an advisory lock on the same job_id is held in a concurrent session — the
    claim must not block or fail.
    """
    from app.queue.sqs import SQSClient
    from app.workers.watcher import claim_and_publish

    now = datetime.now(tz=UTC)
    bucket = now.replace(minute=0, second=0, microsecond=0).isoformat()

    async with session_factory() as session:
        async with session.begin():
            job = Job(
                user_id="watcher-claim-lock-test",
                description="immediate job",
                action="echo",
                action_params={},
                job_type="one_shot",
                scheduled_at=now,
            )
            session.add(job)
            await session.flush()
            run = JobRun(
                time_bucket=bucket,
                job_id=job.job_id,
                user_id=job.user_id,
                scheduled_at=now,
                status="PENDING",
            )
            session.add(run)
            await session.flush()
            session.add(
                RunEvent(
                    run_id=run.run_id,
                    job_id=job.job_id,
                    event_type="CREATED",
                    status_from=None,
                    status_to="PENDING",
                )
            )
        run_id = run.run_id
        job_id = job.job_id

    # Hold an advisory lock on the job_id in a background session while the
    # Watcher tries to claim the PENDING run.  The Watcher uses row-level
    # FOR UPDATE SKIP LOCKED — a completely separate lock mechanism — so it
    # must not be blocked by the advisory lock.
    sqs = SQSClient()
    sqs.send_message_batch = lambda entries: None  # stub out SQS publish

    claimed_count: list[int] = []

    async def _hold_advisory_lock() -> None:
        """Hold pg_advisory_xact_lock on the job_id for a brief window."""
        engine2 = create_async_engine()
        factory2 = async_sessionmaker(engine2, expire_on_commit=False)
        async with factory2() as s2:
            async with s2.begin():
                await s2.execute(
                    text("SELECT pg_advisory_xact_lock(:job_id)"),
                    {"job_id": job_id},
                )
                # Hold the lock just long enough for the Watcher to run.
                await asyncio.sleep(0.05)
        await engine2.dispose()

    async def _run_watcher() -> None:
        count = await claim_and_publish(session_factory, sqs)
        claimed_count.append(count)

    await asyncio.gather(_hold_advisory_lock(), _run_watcher())

    assert claimed_count == [1], (
        f"Watcher must claim exactly 1 run regardless of advisory lock; got {claimed_count}"
    )
    # Confirm the run is now QUEUED.
    async with session_factory() as session:
        async with session.begin():
            claimed_run = (
                await session.execute(select(JobRun).where(JobRun.run_id == run_id))
            ).scalar_one()
    assert claimed_run.status == "QUEUED", (
        f"run must be QUEUED after claim, got {claimed_run.status}"
    )
