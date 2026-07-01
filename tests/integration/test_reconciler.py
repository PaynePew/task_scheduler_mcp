"""Integration tests for app/workers/reconciler.py — requires running Postgres + ElasticMQ.

Run with:
    uv run pytest -m integration tests/integration/test_reconciler.py
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.engine import create_async_engine
from app.db.models import Job, JobRun, RunEvent
from app.domain.run_materializer import has_executing_run
from app.queue.sqs import SQSClient
from app.workers.executor import _claim
from app.workers.reconciler import reconcile_once

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

TINY_GRACE = timedelta(seconds=1)  # very small so test rows always exceed it


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
    """SQSClient pointed at ElasticMQ; drains leftover messages before each test."""
    client = SQSClient()
    while True:
        msgs = client.receive_messages(max_messages=10, wait_seconds=0)
        if not msgs:
            break
        for msg in msgs:
            client.delete_message(msg["ReceiptHandle"])
    return client


async def _insert_job(
    factory: async_sessionmaker,
    *,
    action: str = "echo",
    job_type: str = "one_shot",
    cron_expr: str | None = None,
) -> Job:
    """Insert a bare Job row and return it (committed).

    ``action`` selects the handler whose idempotency posture Sweep C keys on
    (``echo`` is idempotent; ``email_send`` is not). A ``recurring`` job carries
    a ``cron_expr`` and no ``scheduled_at`` (the DB CHECK constraint enforces this).
    """
    scheduled = None if job_type == "recurring" else datetime.now(tz=UTC) - timedelta(seconds=30)
    async with factory() as session:
        async with session.begin():
            job = Job(
                user_id="reconciler-test",
                description="reconciler test job",
                action=action,
                action_params={"message": "test"},
                job_type=job_type,
                scheduled_at=scheduled,
                cron_expr=cron_expr,
            )
            session.add(job)
            await session.flush()
            job_id = job.job_id
    # Re-fetch outside the closed transaction
    async with factory() as session:
        return (await session.execute(select(Job).where(Job.job_id == job_id))).scalar_one()


async def _insert_run(
    factory: async_sessionmaker,
    job: Job,
    *,
    status: str,
    updated_at: datetime,
    scheduled_at: datetime | None = None,
    heartbeat_at: datetime | None = None,
) -> JobRun:
    """Insert a JobRun with explicit status and updated_at (committed).

    ``scheduled_at`` defaults to ``job.scheduled_at`` (falling back to now-30s for
    a recurring job, whose ``Job.scheduled_at`` is NULL).  Pass a distinct value
    when inserting multiple non-terminal runs for the same job to avoid hitting
    the ``uq_job_runs_job_scheduled_nonterminal`` partial unique index.
    ``heartbeat_at`` sets the DB-side lease Sweep C keys on (stale → orphan).
    """
    now_bucket = datetime.now(tz=UTC).replace(minute=0, second=0, microsecond=0)
    effective_scheduled_at = scheduled_at if scheduled_at is not None else job.scheduled_at
    if effective_scheduled_at is None:
        effective_scheduled_at = datetime.now(tz=UTC) - timedelta(seconds=30)
    async with factory() as session:
        async with session.begin():
            run = JobRun(
                time_bucket=now_bucket.isoformat(),
                job_id=job.job_id,
                user_id=job.user_id,
                scheduled_at=effective_scheduled_at,
                status=status,
            )
            session.add(run)
            await session.flush()
            run_id = run.run_id

        # Update updated_at + heartbeat_at via raw SQL to bypass server_default /
        # ORM auto-now logic so the test controls the exact lease age.
        async with session.begin():
            await session.execute(
                update(JobRun)
                .where(JobRun.run_id == run_id)
                .values(updated_at=updated_at, heartbeat_at=heartbeat_at)
            )

    async with factory() as session:
        return (await session.execute(select(JobRun).where(JobRun.run_id == run_id))).scalar_one()


# ---------------------------------------------------------------------------
# Test A — DLQ-reconcile: RETRYING older than grace → FAILED
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_reconcile_a_retrying_past_grace_flipped_to_failed(session_factory, sqs):
    """RETRYING row older than DLQ grace → flipped to FAILED + RunEvent with dlq_reconcile."""
    job = await _insert_job(session_factory)
    old_ts = datetime.now(tz=UTC) - timedelta(seconds=600)
    run = await _insert_run(session_factory, job, status="RETRYING", updated_at=old_ts)

    a, b, c = await reconcile_once(
        session_factory, sqs, dlq_grace=TINY_GRACE, queued_grace=TINY_GRACE
    )

    assert a == 1
    assert b == 0
    assert c == 0

    async with session_factory() as session:
        async with session.begin():
            updated = (
                await session.execute(select(JobRun).where(JobRun.run_id == run.run_id))
            ).scalar_one()
            events = (
                (
                    await session.execute(
                        select(RunEvent).where(
                            RunEvent.run_id == run.run_id, RunEvent.event_type == "FAILED"
                        )
                    )
                )
                .scalars()
                .all()
            )

    assert updated.status == "FAILED"
    assert updated.finish_at is not None
    assert updated.error_message == "exceeded_max_receive (likely DLQ)"

    assert len(events) == 1
    evt = events[0]
    assert evt.status_from == "RETRYING"
    assert evt.status_to == "FAILED"
    assert evt.event_data is not None
    assert evt.event_data["reason"] == "dlq_reconcile"
    # last_seen_at must be the pre-UPDATE stale timestamp, not the reconcile
    # time. Guards against the SQLAlchemy synchronize_session="auto" anti-pattern
    # where reading run.updated_at after a bulk UPDATE returns the new value.
    last_seen = datetime.fromisoformat(evt.event_data["last_seen_at"])
    assert last_seen < datetime.now(tz=UTC) - timedelta(seconds=60)


# ---------------------------------------------------------------------------
# Test B — QUEUED-reconcile: stuck QUEUED → SQS message + RunEvent(REENQUEUED)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_reconcile_b_queued_past_grace_reenqueued(session_factory, sqs):
    """QUEUED row older than grace → SQS message sent + RunEvent(REENQUEUED)."""
    job = await _insert_job(session_factory)
    old_ts = datetime.now(tz=UTC) - timedelta(seconds=600)
    run = await _insert_run(session_factory, job, status="QUEUED", updated_at=old_ts)

    a, b, c = await reconcile_once(
        session_factory, sqs, dlq_grace=TINY_GRACE, queued_grace=TINY_GRACE
    )

    assert a == 0
    assert b == 1
    assert c == 0

    # SQS must have the message
    msgs = sqs.receive_messages(max_messages=10, wait_seconds=0)
    assert len(msgs) == 1
    body = json.loads(msgs[0]["Body"])
    assert body["run_id"] == run.run_id
    assert body["job_id"] == job.job_id

    async with session_factory() as session:
        async with session.begin():
            updated = (
                await session.execute(select(JobRun).where(JobRun.run_id == run.run_id))
            ).scalar_one()
            events = (
                (
                    await session.execute(
                        select(RunEvent).where(
                            RunEvent.run_id == run.run_id, RunEvent.event_type == "REENQUEUED"
                        )
                    )
                )
                .scalars()
                .all()
            )

    # Status must still be QUEUED (only updated_at advances)
    assert updated.status == "QUEUED"
    # updated_at must have advanced past the old stale value
    assert updated.updated_at > old_ts

    assert len(events) == 1
    evt = events[0]
    assert evt.status_from == "QUEUED"
    assert evt.status_to == "QUEUED"
    assert evt.event_data is not None
    assert evt.event_data["reason"] == "watcher_send_failed"


# ---------------------------------------------------------------------------
# Test D — Sweep B scheduled_at guard: future-scheduled QUEUED rows are left alone
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_reconcile_b_future_scheduled_queued_not_reenqueued(session_factory, sqs):
    """QUEUED row with stale updated_at but a FUTURE scheduled_at is left alone.

    The Watcher legitimately queues a run up to `lookahead window` (5 min) ahead
    of scheduled_at with an SQS DelaySeconds, so it can sit QUEUED for minutes
    with no worker activity. Sweep B must not treat that healthy wait as stuck.
    """
    job = await _insert_job(session_factory)
    old_updated_at = datetime.now(tz=UTC) - timedelta(seconds=600)
    future_scheduled_at = datetime.now(tz=UTC) + timedelta(minutes=3)
    run = await _insert_run(
        session_factory,
        job,
        status="QUEUED",
        updated_at=old_updated_at,
        scheduled_at=future_scheduled_at,
    )

    a, b, c = await reconcile_once(
        session_factory, sqs, dlq_grace=TINY_GRACE, queued_grace=TINY_GRACE
    )

    assert a == 0
    assert b == 0, "a future-scheduled QUEUED row must not be re-enqueued early"
    assert c == 0

    # No SQS message should have been sent
    msgs = sqs.receive_messages(max_messages=10, wait_seconds=0)
    assert msgs == [], "no SQS messages should have been sent for a future-scheduled row"

    async with session_factory() as session:
        async with session.begin():
            updated = (
                await session.execute(select(JobRun).where(JobRun.run_id == run.run_id))
            ).scalar_one()
            events = (
                (
                    await session.execute(
                        select(RunEvent).where(
                            RunEvent.run_id == run.run_id, RunEvent.event_type == "REENQUEUED"
                        )
                    )
                )
                .scalars()
                .all()
            )

    assert updated.status == "QUEUED"
    # updated_at must remain untouched — the row was never even considered.
    assert updated.updated_at == old_updated_at
    assert events == []


@pytest.mark.integration
async def test_reconcile_b_past_due_queued_still_reenqueued(session_factory, sqs):
    """A genuinely stuck (past-due, watcher-send-failed) QUEUED row is still re-enqueued.

    Regression guard for the scheduled_at guard: it must not swallow the
    original Sweep B behavior for past-due rows.
    """
    job = await _insert_job(session_factory)
    old_ts = datetime.now(tz=UTC) - timedelta(seconds=600)
    past_scheduled_at = datetime.now(tz=UTC) - timedelta(seconds=60)
    run = await _insert_run(
        session_factory,
        job,
        status="QUEUED",
        updated_at=old_ts,
        scheduled_at=past_scheduled_at,
    )

    a, b, c = await reconcile_once(
        session_factory, sqs, dlq_grace=TINY_GRACE, queued_grace=TINY_GRACE
    )

    assert a == 0
    assert b == 1, "a past-due stuck QUEUED row must still be re-enqueued"
    assert c == 0

    msgs = sqs.receive_messages(max_messages=10, wait_seconds=0)
    assert len(msgs) == 1
    body = json.loads(msgs[0]["Body"])
    assert body["run_id"] == run.run_id
    # Delete so the message doesn't linger invisible and reappear in a later
    # test once its visibility timeout expires (cross-test contamination).
    sqs.delete_message(msgs[0]["ReceiptHandle"])

    async with session_factory() as session:
        async with session.begin():
            updated = (
                await session.execute(select(JobRun).where(JobRun.run_id == run.run_id))
            ).scalar_one()

    assert updated.status == "QUEUED"
    assert updated.updated_at > old_ts


# ---------------------------------------------------------------------------
# Test C — no false positives: fresh rows inside the grace window are not touched
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_reconcile_c_no_false_positives(session_factory, sqs):
    """Rows inside the grace window are not touched by either sweep."""
    job = await _insert_job(session_factory)

    # Both rows have updated_at = now → inside any grace window > 0.
    # Use distinct scheduled_at values: the unique index prevents two non-terminal
    # runs sharing the same (job_id, scheduled_at).
    recent_ts = datetime.now(tz=UTC)
    retrying_run = await _insert_run(session_factory, job, status="RETRYING", updated_at=recent_ts)
    queued_run = await _insert_run(
        session_factory,
        job,
        status="QUEUED",
        updated_at=recent_ts,
        scheduled_at=job.scheduled_at + timedelta(seconds=60),
    )

    # Use a large grace window so "recent_ts" is definitely inside it
    big_grace = timedelta(hours=1)
    a, b, c = await reconcile_once(
        session_factory, sqs, dlq_grace=big_grace, queued_grace=big_grace
    )

    assert a == 0, "no RETRYING row should have been touched"
    assert b == 0, "no QUEUED row should have been touched"
    assert c == 0, "no RUNNING row should have been touched"

    async with session_factory() as session:
        async with session.begin():
            r_run = (
                await session.execute(select(JobRun).where(JobRun.run_id == retrying_run.run_id))
            ).scalar_one()
            q_run = (
                await session.execute(select(JobRun).where(JobRun.run_id == queued_run.run_id))
            ).scalar_one()

    assert r_run.status == "RETRYING", "fresh RETRYING row must remain untouched"
    assert q_run.status == "QUEUED", "fresh QUEUED row must remain untouched"

    # SQS queue must remain empty
    msgs = sqs.receive_messages(max_messages=10, wait_seconds=0)
    assert msgs == [], "no SQS messages should have been sent for in-grace rows"


# ---------------------------------------------------------------------------
# Sweep C — RUNNING-orphan recovery (issue #271 / PRD #266)
# ---------------------------------------------------------------------------

RUNNING_GRACE = timedelta(seconds=1)  # tiny so a stale-heartbeat RUNNING row always exceeds it


@pytest.mark.integration
async def test_reconcile_c_running_orphan_non_idempotent_failed(session_factory, sqs):
    """A stale RUNNING orphan of a NON-idempotent action -> FAILED + alert + settle.

    email_send is non-idempotent (a crashed worker may or may not have sent the
    mail), so Sweep C must NOT blind-retry: it flips the row to FAILED with a
    running_orphan RunEvent, and settles the one-shot so its quota slot frees.
    """
    job = await _insert_job(session_factory, action="email_send")
    stale_hb = datetime.now(tz=UTC) - timedelta(seconds=600)
    run = await _insert_run(
        session_factory, job, status="RUNNING", updated_at=stale_hb, heartbeat_at=stale_hb
    )

    a, b, c = await reconcile_once(
        session_factory,
        sqs,
        dlq_grace=TINY_GRACE,
        queued_grace=TINY_GRACE,
        running_grace=RUNNING_GRACE,
    )

    assert a == 0
    assert b == 0
    assert c == 1

    async with session_factory() as session:
        async with session.begin():
            updated = (
                await session.execute(select(JobRun).where(JobRun.run_id == run.run_id))
            ).scalar_one()
            events = (
                (
                    await session.execute(
                        select(RunEvent).where(
                            RunEvent.run_id == run.run_id, RunEvent.event_type == "FAILED"
                        )
                    )
                )
                .scalars()
                .all()
            )
            refreshed_job = (
                await session.execute(select(Job).where(Job.job_id == job.job_id))
            ).scalar_one()
            wedged = await has_executing_run(session, job.job_id)

    assert updated.status == "FAILED"
    assert updated.finish_at is not None
    assert updated.error_message is not None and "running_orphan" in updated.error_message

    assert len(events) == 1
    evt = events[0]
    assert evt.status_from == "RUNNING"
    assert evt.status_to == "FAILED"
    assert evt.event_data is not None
    assert evt.event_data["reason"] == "running_orphan"

    # Job un-wedged + quota freed: the one-shot settled to completed and no longer
    # has an executing run, so has_executing_run (the forbid-concurrency gate) clears.
    assert refreshed_job.state == "completed"
    assert wedged is False

    # Non-idempotent -> NO re-enqueue.
    msgs = sqs.receive_messages(max_messages=10, wait_seconds=0)
    assert msgs == [], "a non-idempotent orphan must not be re-enqueued"


@pytest.mark.integration
async def test_reconcile_c_running_orphan_idempotent_reset_and_reenqueued(session_factory, sqs):
    """A stale RUNNING orphan of an IDEMPOTENT action -> reset to QUEUED + re-enqueued.

    echo is idempotent, so Sweep C resets the row to a *claimable* status and
    re-sends the message. The reset is the whole point: a re-sent message that hit
    a still-RUNNING row would be deleted as a duplicate and re-orphan the run. The
    re-sent message must be claimable -- verified by driving the real executor claim.
    """
    job = await _insert_job(session_factory, action="echo")
    stale_hb = datetime.now(tz=UTC) - timedelta(seconds=600)
    run = await _insert_run(
        session_factory, job, status="RUNNING", updated_at=stale_hb, heartbeat_at=stale_hb
    )

    a, b, c = await reconcile_once(
        session_factory,
        sqs,
        dlq_grace=TINY_GRACE,
        queued_grace=TINY_GRACE,
        running_grace=RUNNING_GRACE,
    )

    assert a == 0
    assert b == 0
    assert c == 1

    # Row reset to a claimable status.
    async with session_factory() as session:
        async with session.begin():
            updated = (
                await session.execute(select(JobRun).where(JobRun.run_id == run.run_id))
            ).scalar_one()
            events = (
                (
                    await session.execute(
                        select(RunEvent).where(
                            RunEvent.run_id == run.run_id, RunEvent.event_type == "REENQUEUED"
                        )
                    )
                )
                .scalars()
                .all()
            )
    assert updated.status == "QUEUED"
    assert updated.finish_at is None, "an idempotent orphan is re-run, not terminal"
    assert len(events) == 1
    assert events[0].status_from == "RUNNING"
    assert events[0].status_to == "QUEUED"
    assert events[0].event_data["reason"] == "running_orphan"

    # The re-sent message is present and carries this run.
    msgs = sqs.receive_messages(max_messages=10, wait_seconds=0)
    assert len(msgs) == 1
    body = json.loads(msgs[0]["Body"])
    assert body["run_id"] == run.run_id
    assert body["job_id"] == job.job_id
    # Delete so it doesn't reappear (invisible->visible) and contaminate later tests.
    sqs.delete_message(msgs[0]["ReceiptHandle"])

    # The reset row is genuinely claimable -- the real executor claim wins on it.
    async with session_factory() as session:
        async with session.begin():
            claimed = await _claim(session, run.run_id, job.job_id)
    assert claimed is True, "the reset-to-QUEUED row must be re-claimable by a worker"


@pytest.mark.integration
async def test_reconcile_c_fresh_heartbeat_running_not_touched(session_factory, sqs):
    """A RUNNING row with a FRESH heartbeat lease is left alone (live long-running worker).

    Guards the core no-false-positive invariant: a slow-but-alive worker bumps
    heartbeat_at within grace, so Sweep C must never sweep it.
    """
    job = await _insert_job(session_factory, action="email_send")
    fresh_hb = datetime.now(tz=UTC)
    run = await _insert_run(
        session_factory, job, status="RUNNING", updated_at=fresh_hb, heartbeat_at=fresh_hb
    )

    a, b, c = await reconcile_once(
        session_factory,
        sqs,
        dlq_grace=TINY_GRACE,
        queued_grace=TINY_GRACE,
        running_grace=timedelta(minutes=5),
    )

    assert a == 0
    assert b == 0
    assert c == 0, "a fresh-heartbeat RUNNING row must not be swept"

    async with session_factory() as session:
        async with session.begin():
            updated = (
                await session.execute(select(JobRun).where(JobRun.run_id == run.run_id))
            ).scalar_one()
    assert updated.status == "RUNNING", "a live long-running worker's row must stay RUNNING"

    msgs = sqs.receive_messages(max_messages=10, wait_seconds=0)
    assert msgs == [], "no SQS messages should have been sent for a live RUNNING row"


@pytest.mark.integration
async def test_reconcile_c_recurring_orphan_unwedges_without_settling_root(session_factory, sqs):
    """A recurring job's stale RUNNING orphan -> FAILED, root stays active, un-wedged.

    A recurring root never auto-settles (ADR-068), so its Job.state stays active;
    but the orphan must still reach a terminal status so has_executing_run clears
    and the continuation consumer can materialize the next tick -- otherwise the
    recurrence is wedged forever.
    """
    job = await _insert_job(
        session_factory, action="email_send", job_type="recurring", cron_expr="0 * * * *"
    )
    stale_hb = datetime.now(tz=UTC) - timedelta(seconds=600)
    run = await _insert_run(
        session_factory, job, status="RUNNING", updated_at=stale_hb, heartbeat_at=stale_hb
    )

    a, b, c = await reconcile_once(
        session_factory,
        sqs,
        dlq_grace=TINY_GRACE,
        queued_grace=TINY_GRACE,
        running_grace=RUNNING_GRACE,
    )

    assert c == 1

    async with session_factory() as session:
        async with session.begin():
            updated = (
                await session.execute(select(JobRun).where(JobRun.run_id == run.run_id))
            ).scalar_one()
            refreshed_job = (
                await session.execute(select(Job).where(Job.job_id == job.job_id))
            ).scalar_one()
            wedged = await has_executing_run(session, job.job_id)

    assert updated.status == "FAILED"
    # Recurring root is NOT auto-settled -- only cancel ends it (ADR-068).
    assert refreshed_job.state == "active"
    # But the wedge is cleared: no executing run blocks the next recurring successor.
    assert wedged is False
