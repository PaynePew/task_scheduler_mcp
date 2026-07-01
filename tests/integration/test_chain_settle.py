"""Integration tests for trigger-driven Job.state settle + cascade (ADR-068 §2, #255).

A chained (trigger-driven) job settles ``active → completed`` when its trigger
parent is terminal (``completed``/``cancelled``) **and** it has no non-terminal
run — one-hop parent propagation driven by the continuation consumer. Cancelling a
job cascades that settle to its downstreams, so a cancelled upstream stops its
not-yet-run downstreams and frees their quota (which counts ``state='active'``).

Chains are seeded directly (bypassing create_job's V3, which rejects chaining off a
job with no non-terminal run) so each topology and state is set exactly; the settle
paths under test are the real ones — ``cancel_job`` and the continuation consumer's
``poll_once`` → ``settle_check``.

Run: uv run pytest -m integration tests/integration/test_chain_settle.py
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.engine import create_async_engine
from app.db.models import Job, JobRun, RunEvent
from app.domain.jobs import cancel_job, settle_job
from app.workers.recurring_watcher import poll_once as continuation_poll_once

_USER = "settle-test-user"


@pytest_asyncio.fixture
async def session_factory() -> async_sessionmaker[AsyncSession]:
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
# Seeding helpers — build exact chain topologies without create_job's V3 guard.
# ---------------------------------------------------------------------------


async def _seed_job(
    factory: async_sessionmaker[AsyncSession],
    *,
    job_type: str = "one_shot",
    cron_expr: str | None = None,
    trigger_on_job_id: int | None = None,
    trigger_on_status: str | None = None,
    state: str = "active",
) -> int:
    """Insert one Job; return its job_id. Satisfies ck_jobs_schedule_consistency."""
    now = datetime.now(tz=UTC)
    async with factory() as session:
        async with session.begin():
            job = Job(
                user_id=_USER,
                description="settle-test",
                action="echo",
                action_params={},
                job_type=job_type,
                scheduled_at=None if cron_expr else now,
                cron_expr=cron_expr,
                timezone="UTC",
                trigger_on_job_id=trigger_on_job_id,
                trigger_on_status=trigger_on_status,
                state=state,
            )
            session.add(job)
            await session.flush()
            return job.job_id


async def _seed_run(
    factory: async_sessionmaker[AsyncSession],
    job_id: int,
    status: str,
    *,
    emit_terminal_event: bool = False,
) -> int:
    """Insert one JobRun for job_id; optionally emit an unprocessed terminal event."""
    now = datetime.now(tz=UTC)
    bucket = now.replace(minute=0, second=0, microsecond=0).isoformat()
    async with factory() as session:
        async with session.begin():
            run = JobRun(
                time_bucket=bucket,
                job_id=job_id,
                user_id=_USER,
                scheduled_at=now,
                status=status,
                finish_at=now if status in ("SUCCEEDED", "FAILED", "CANCELLED") else None,
            )
            session.add(run)
            await session.flush()
            if emit_terminal_event:
                session.add(
                    RunEvent(
                        run_id=run.run_id,
                        job_id=job_id,
                        event_type=status,
                        status_from="RUNNING",
                        status_to=status,
                        occurred_at=now,
                    )
                )
            return run.run_id


async def _terminate_run(
    factory: async_sessionmaker[AsyncSession],
    *,
    run_id: int,
    job_id: int,
    status: str = "SUCCEEDED",
) -> None:
    """Mimic the executor writing a run's terminal status: set status, emit the
    terminal RunEvent, and call the non-cascading self-settle (settle_job)."""
    async with factory() as session:
        async with session.begin():
            await session.execute(
                text("UPDATE job_runs SET status = :s, finish_at = now() WHERE run_id = :r"),
                {"s": status, "r": run_id},
            )
            session.add(
                RunEvent(
                    run_id=run_id,
                    job_id=job_id,
                    event_type=status,
                    status_from="RUNNING",
                    status_to=status,
                )
            )
            await settle_job(session, job_id=job_id)


async def _state(factory: async_sessionmaker[AsyncSession], job_id: int) -> str:
    async with factory() as session:
        return (await session.execute(select(Job.state).where(Job.job_id == job_id))).scalar_one()


async def _latest_run(factory: async_sessionmaker[AsyncSession], job_id: int) -> JobRun | None:
    async with factory() as session:
        return (
            await session.execute(
                select(JobRun)
                .where(JobRun.job_id == job_id)
                .order_by(JobRun.run_id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()


async def _active_count(factory: async_sessionmaker[AsyncSession]) -> int:
    """The quota predicate: number of jobs still counting as active load."""
    async with factory() as session:
        return (
            await session.execute(
                select(func.count()).select_from(Job).where(Job.state == "active")
            )
        ).scalar_one()


# ---------------------------------------------------------------------------
# Parent propagation: a chained downstream settles after its run terminates.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_chained_downstream_settles_after_its_run_completes(session_factory):
    """A → B (ANY). A completed with an unprocessed SUCCEEDED event; the continuation
    consumer creates B's run (B stays active), and B settles to completed only once
    its own run terminates and the parent is terminal (ADR-068 §2)."""
    a = await _seed_job(session_factory, state="completed")
    a_run = await _seed_run(session_factory, a, "SUCCEEDED", emit_terminal_event=True)
    b = await _seed_job(session_factory, trigger_on_job_id=a, trigger_on_status="ANY")

    # Continuation reacts to A's terminal event → creates B's PENDING run.
    await continuation_poll_once(session_factory)
    b_run = await _latest_run(session_factory, b)
    assert b_run is not None and b_run.status == "PENDING"
    assert b_run.wait_for_run_id == a_run, "downstream run carries the upstream terminal run_id"
    # B has a live run → resident load → NOT settled yet.
    assert await _state(session_factory, b) == "active"

    # B's run finishes; settle_job(B) no-ops (chained) — continuation settles it.
    await _terminate_run(session_factory, run_id=b_run.run_id, job_id=b)
    assert await _state(session_factory, b) == "active", "chained settle is the consumer's job"
    await continuation_poll_once(session_factory)
    assert await _state(session_factory, b) == "completed"


@pytest.mark.integration
async def test_predicate_miss_settles_never_run_downstream(session_factory):
    """A succeeds but B triggers on FAILED (predicate miss): B never gets a run, and
    the continuation consumer settles it to completed since the parent is terminal."""
    a = await _seed_job(session_factory, state="completed")
    await _seed_run(session_factory, a, "SUCCEEDED", emit_terminal_event=True)
    b = await _seed_job(session_factory, trigger_on_job_id=a, trigger_on_status="FAILED")

    await continuation_poll_once(session_factory)

    assert await _latest_run(session_factory, b) is None, "predicate miss creates no run"
    assert await _state(session_factory, b) == "completed"


# ---------------------------------------------------------------------------
# Cancel cascade: a cancelled upstream stops its not-yet-run downstreams.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_cancel_upstream_settles_not_yet_run_downstream(session_factory):
    """Cancelling A settles its not-yet-run downstream B to completed (cascade), frees
    quota, and the continuation consumer never creates a run for the cancelled chain."""
    a = await _seed_job(session_factory)
    await _seed_run(session_factory, a, "PENDING")
    b = await _seed_job(session_factory, trigger_on_job_id=a, trigger_on_status="ANY")
    assert await _active_count(session_factory) == 2

    async with session_factory() as session:
        await cancel_job(session, user_id=_USER, job_id=a)

    assert await _state(session_factory, a) == "cancelled"
    assert await _state(session_factory, b) == "completed", "cancelled upstream stops downstream"
    assert await _active_count(session_factory) == 0, "quota reflects the cascade"

    # The continuation consumer draining A's CANCELLED event must NOT create a B run.
    await continuation_poll_once(session_factory)
    assert await _latest_run(session_factory, b) is None


@pytest.mark.integration
async def test_cancel_upstream_cascades_multi_hop(session_factory):
    """A → B → C, none yet run. Cancelling A cascades settle down the whole chain."""
    a = await _seed_job(session_factory)
    await _seed_run(session_factory, a, "PENDING")
    b = await _seed_job(session_factory, trigger_on_job_id=a, trigger_on_status="ANY")
    c = await _seed_job(session_factory, trigger_on_job_id=b, trigger_on_status="ANY")

    async with session_factory() as session:
        await cancel_job(session, user_id=_USER, job_id=a)

    assert await _state(session_factory, a) == "cancelled"
    assert await _state(session_factory, b) == "completed"
    assert await _state(session_factory, c) == "completed"
    assert await _active_count(session_factory) == 0


@pytest.mark.integration
async def test_cancel_upstream_leaves_in_flight_downstream_to_finish(session_factory):
    """B has a RUNNING run when A is cancelled: B is NOT settled (in-flight resident
    load is left to finish); it settles only once that run terminates."""
    a = await _seed_job(session_factory)
    await _seed_run(session_factory, a, "PENDING")
    b = await _seed_job(session_factory, trigger_on_job_id=a, trigger_on_status="ANY")
    b_run = await _seed_run(session_factory, b, "RUNNING")

    async with session_factory() as session:
        await cancel_job(session, user_id=_USER, job_id=a)

    assert await _state(session_factory, a) == "cancelled"
    # In-flight downstream run left to finish → B stays active for now.
    assert await _state(session_factory, b) == "active"

    # The run finishes; the continuation consumer then settles B (parent terminal).
    await _terminate_run(session_factory, run_id=b_run, job_id=b)
    await continuation_poll_once(session_factory)
    assert await _state(session_factory, b) == "completed"


@pytest.mark.integration
async def test_settle_check_does_not_settle_downstream_of_active_parent(session_factory):
    """A chained B whose parent A is still active (e.g. recurring) must NOT settle,
    even with no live run — the parent can still fire another upstream run."""
    a = await _seed_job(session_factory, job_type="recurring", cron_expr="0 8 * * *")
    b = await _seed_job(session_factory, trigger_on_job_id=a, trigger_on_status="ANY")

    # Drive settle_check directly on B (no live run, parent active).
    from app.domain.jobs import settle_check

    async with session_factory() as session:
        async with session.begin():
            settled = await settle_check(session, job_id=b)

    assert settled is False, "must not settle a downstream while its parent is active"
    assert await _state(session_factory, b) == "active"
