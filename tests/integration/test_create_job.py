"""Integration tests for domain.create_job against a real Postgres instance.

Run with:
    docker compose up -d postgres && alembic upgrade head
    uv run pytest -m integration tests/integration/test_create_job.py
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.engine import create_async_engine
from app.db.models import Job, JobRun, RunEvent
from app.domain.chain_validation import (
    ChainJobNotFoundError,
    ChainJobTerminatedError,
)
from app.domain.jobs import (
    InvalidScheduledAtError,
    InvalidTimezoneError,
    UnknownActionError,
    create_job,
)


@pytest_asyncio.fixture
async def session_factory():
    """Fresh engine per test — avoids cross-event-loop pool reuse."""
    engine = create_async_engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    # Clean up all test data
    async with factory() as session:
        async with session.begin():
            await session.execute(text("DELETE FROM run_events"))
            await session.execute(text("DELETE FROM job_runs"))
            await session.execute(text("DELETE FROM jobs"))
    await engine.dispose()


@pytest.mark.integration
async def test_immediate_echo_creates_one_job_run_event(session_factory):
    """Creating an immediate echo job inserts 1 Job + 1 JobRun(PENDING) + 1 RunEvent(CREATED)."""
    async with session_factory() as session:
        job = await create_job(
            session,
            user_id="test-user",
            action="echo",
            action_params={"message": "hello"},
            schedule_type="immediate",
        )

    job_id = job.job_id

    async with session_factory() as session:
        async with session.begin():
            job_count = await session.scalar(
                select(func.count()).select_from(Job).where(Job.job_id == job_id)
            )
            runs = (
                (await session.execute(select(JobRun).where(JobRun.job_id == job_id)))
                .scalars()
                .all()
            )
            events = (
                (await session.execute(select(RunEvent).where(RunEvent.job_id == job_id)))
                .scalars()
                .all()
            )

    assert job_count == 1
    assert len(runs) == 1
    assert runs[0].status == "PENDING"
    assert len(events) == 1
    assert events[0].event_type == "CREATED"


@pytest.mark.integration
async def test_idempotency_same_user_returns_existing_job(session_factory):
    """Second call with same (user_id, idempotency_key) returns same job_id; rows unchanged."""
    async with session_factory() as session:
        job1 = await create_job(
            session,
            user_id="idem-user",
            action="echo",
            action_params={"message": "first"},
            schedule_type="immediate",
            idempotency_key="key-abc",
        )

    async with session_factory() as session:
        job2 = await create_job(
            session,
            user_id="idem-user",
            action="echo",
            action_params={"message": "second"},
            schedule_type="immediate",
            idempotency_key="key-abc",
        )

    assert job1.job_id == job2.job_id

    async with session_factory() as session:
        async with session.begin():
            run_count = await session.scalar(
                select(func.count()).select_from(JobRun).where(JobRun.job_id == job1.job_id)
            )
            event_count = await session.scalar(
                select(func.count()).select_from(RunEvent).where(RunEvent.job_id == job1.job_id)
            )

    assert run_count == 1
    assert event_count == 1


@pytest.mark.integration
async def test_different_user_same_idempotency_key_creates_new_job(session_factory):
    """Different user_id + same idempotency_key creates a distinct new Job."""
    async with session_factory() as session:
        job_a = await create_job(
            session,
            user_id="user-A",
            action="echo",
            action_params={"message": "a"},
            schedule_type="immediate",
            idempotency_key="shared-key",
        )

    async with session_factory() as session:
        job_b = await create_job(
            session,
            user_id="user-B",
            action="echo",
            action_params={"message": "b"},
            schedule_type="immediate",
            idempotency_key="shared-key",
        )

    assert job_a.job_id != job_b.job_id


@pytest.mark.integration
async def test_unknown_action_raises_domain_error(session_factory):
    """Unknown action raises UnknownActionError (mappable to UNKNOWN_ACTION)."""
    async with session_factory() as session:
        with pytest.raises(UnknownActionError):
            await create_job(
                session,
                user_id="test-user",
                action="not_registered",
                action_params={},
                schedule_type="immediate",
            )


# ---------------------------------------------------------------------------
# one-shot scheduling integration tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_one_shot_future_creates_pending_run(session_factory):
    """one-shot job 2 min in future → PENDING run + CREATED event at the right scheduled_at."""
    future = datetime.now(tz=UTC) + timedelta(minutes=2)
    future_str = future.isoformat()

    async with session_factory() as session:
        job = await create_job(
            session,
            user_id="oneshot-user",
            action="echo",
            action_params={"message": "scheduled"},
            schedule_type="one-shot",
            scheduled_at=future_str,
            timezone="UTC",
        )

    job_id = job.job_id

    async with session_factory() as session:
        async with session.begin():
            job_count = await session.scalar(
                select(func.count()).select_from(Job).where(Job.job_id == job_id)
            )
            runs = (
                (await session.execute(select(JobRun).where(JobRun.job_id == job_id)))
                .scalars()
                .all()
            )
            events = (
                (await session.execute(select(RunEvent).where(RunEvent.job_id == job_id)))
                .scalars()
                .all()
            )

    assert job_count == 1
    assert len(runs) == 1
    assert runs[0].status == "PENDING"
    # scheduled_at stored in DB must be within a second of the future time we asked for
    delta = abs((runs[0].scheduled_at - future).total_seconds())
    assert delta < 2, f"scheduled_at drifted by {delta}s"
    assert len(events) == 1
    assert events[0].event_type == "CREATED"


@pytest.mark.integration
async def test_one_shot_past_scheduled_at_raises(session_factory):
    """one-shot with past scheduled_at raises InvalidScheduledAtError — no rows written."""
    past = (datetime.now(tz=UTC) - timedelta(seconds=30)).isoformat()

    async with session_factory() as session:
        with pytest.raises(InvalidScheduledAtError, match="must be in the future"):
            await create_job(
                session,
                user_id="oneshot-user",
                action="echo",
                action_params={"message": "late"},
                schedule_type="one-shot",
                scheduled_at=past,
                timezone="UTC",
            )


@pytest.mark.integration
async def test_one_shot_invalid_timezone_raises(session_factory):
    """one-shot with an invalid IANA timezone raises InvalidTimezoneError — no rows written."""
    future = (datetime.now(tz=UTC) + timedelta(hours=1)).isoformat()

    async with session_factory() as session:
        with pytest.raises(InvalidTimezoneError):
            await create_job(
                session,
                user_id="oneshot-user",
                action="echo",
                action_params={"message": "tz-bad"},
                schedule_type="one-shot",
                scheduled_at=future,
                timezone="UTC+8",
            )


@pytest.mark.integration
async def test_one_shot_timezone_normalises_to_utc(session_factory):
    """scheduled_at expressed in Asia/Taipei (+08:00) is stored as UTC in the DB."""
    # Build a naive ISO string far enough in the future that, after being
    # reinterpreted in Asia/Taipei (UTC+8) and converted back to UTC, it still
    # lies in the future of the validation `now`. 9h in UTC strips to a naive
    # wall-clock that, read as Taipei, lands ~1h in the future of UTC `now`.
    base_utc = (datetime.now(tz=UTC) + timedelta(hours=9)).replace(
        minute=0, second=0, microsecond=0
    )
    naive_future = base_utc.replace(tzinfo=None)
    naive_str = naive_future.isoformat()

    async with session_factory() as session:
        job = await create_job(
            session,
            user_id="tz-user",
            action="echo",
            action_params={"message": "taipei"},
            schedule_type="one-shot",
            scheduled_at=naive_str,
            timezone="Asia/Taipei",
        )

    async with session_factory() as session:
        async with session.begin():
            runs = (
                (await session.execute(select(JobRun).where(JobRun.job_id == job.job_id)))
                .scalars()
                .all()
            )

    assert len(runs) == 1
    stored = runs[0].scheduled_at
    # Stored value must be UTC-aware
    assert stored.tzinfo is not None
    # The naive input, interpreted as Asia/Taipei, must normalise to the same
    # UTC instant in the DB (within round-trip microsecond drift).
    expected_utc = naive_future.replace(tzinfo=ZoneInfo("Asia/Taipei")).astimezone(UTC)
    delta = abs((stored - expected_utc).total_seconds())
    assert delta < 2, f"UTC normalisation off by {delta}s"


# ---------------------------------------------------------------------------
# Chain creation: V2 cross-user → NOT_FOUND (not 403)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_chain_v2_cross_user_is_not_found(session_factory):
    """V2: trigger job owned by a different user → ChainJobNotFoundError (404 not 403)."""
    future = (datetime.now(tz=UTC) + timedelta(hours=1)).isoformat()

    # Create trigger job as user-A
    async with session_factory() as session:
        trigger_job = await create_job(
            session,
            user_id="user-a",
            action="echo",
            action_params={"message": "upstream"},
            schedule_type="one-shot",
            scheduled_at=future,
        )

    # user-B tries to chain on user-A's job → ChainJobNotFoundError
    future2 = (datetime.now(tz=UTC) + timedelta(hours=2)).isoformat()
    async with session_factory() as session:
        with pytest.raises(ChainJobNotFoundError):
            await create_job(
                session,
                user_id="user-b",
                action="echo",
                action_params={"message": "chained"},
                schedule_type="one-shot",
                scheduled_at=future2,
                trigger_on_job_id=trigger_job.job_id,
            )


@pytest.mark.integration
async def test_chain_v3_terminated_trigger_job_is_rejected(session_factory):
    """V3: chaining on a fully-terminated trigger job raises ChainJobTerminatedError."""
    # Create trigger job for user-A and manually mark its run SUCCEEDED
    async with session_factory() as session:
        trigger_job = await create_job(
            session,
            user_id="user-a",
            action="echo",
            action_params={"message": "upstream"},
            schedule_type="immediate",
        )

    # Force the run to SUCCEEDED status
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                update(JobRun).where(JobRun.job_id == trigger_job.job_id).values(status="SUCCEEDED")
            )

    # Attempt to chain on the now-terminated job
    future2 = (datetime.now(tz=UTC) + timedelta(hours=2)).isoformat()
    async with session_factory() as session:
        with pytest.raises(ChainJobTerminatedError):
            await create_job(
                session,
                user_id="user-a",
                action="echo",
                action_params={"message": "chained"},
                schedule_type="one-shot",
                scheduled_at=future2,
                trigger_on_job_id=trigger_job.job_id,
            )


@pytest.mark.integration
async def test_chain_creates_waiting_run_with_wait_for_run_id(session_factory):
    """Chained job gets a WAITING run pointing at the upstream's active run."""
    future2 = (datetime.now(tz=UTC) + timedelta(hours=2)).isoformat()

    # Create upstream job (immediate, so run starts as PENDING)
    async with session_factory() as session:
        upstream = await create_job(
            session,
            user_id="chain-create-test",
            action="echo",
            action_params={"message": "upstream"},
            schedule_type="immediate",
        )

    # Get upstream run_id
    async with session_factory() as session:
        async with session.begin():
            upstream_run = (
                await session.execute(select(JobRun).where(JobRun.job_id == upstream.job_id))
            ).scalar_one()

    # Create downstream chained job
    async with session_factory() as session:
        downstream = await create_job(
            session,
            user_id="chain-create-test",
            action="echo",
            action_params={"message": "downstream"},
            schedule_type="one-shot",
            scheduled_at=future2,
            trigger_on_job_id=upstream.job_id,
            trigger_on_status="SUCCEEDED",
        )

    async with session_factory() as session:
        async with session.begin():
            downstream_run = (
                await session.execute(select(JobRun).where(JobRun.job_id == downstream.job_id))
            ).scalar_one()

    assert downstream_run.status == "WAITING"
    assert downstream_run.wait_for_run_id == upstream_run.run_id
