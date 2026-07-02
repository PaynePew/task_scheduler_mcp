"""Integration regression tests for the from_run_id IDOR / cross-tenant guard.

``read_upstream`` scopes its query by user_id (fix/security-hardening-input-abuse):
``from_run_id`` is a data-plane field that bypasses the control-plane ownership
check (chain_validation V2), and ``JobRun.run_id`` is an enumerable autoincrement
PK. Without the scope, user A could set from_run_id to user B's run_id and read
B's JobRun.result. The owner still reads their own run; a cross-tenant read
resolves to NoResult (a 404-equivalent, NOT an error) to prevent run_id enumeration.

Requires running Postgres (DATABASE_URL set in environment).

Run with:
    uv run pytest -m integration tests/integration/test_upstream_reader_ownership.py
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.chain.upstream_reader import NoResult, Ok, read_upstream
from app.db.engine import create_async_engine
from app.db.models import Job, JobRun

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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


async def _insert_run(
    factory: async_sessionmaker[AsyncSession],
    *,
    user_id: str,
    result: str | None = None,
) -> int:
    """Insert a Job + JobRun owned by *user_id* and return the committed run_id."""
    scheduled = datetime.now(tz=UTC) - timedelta(hours=1)
    bucket = scheduled.replace(minute=0, second=0, microsecond=0).isoformat()

    async with factory() as session:
        async with session.begin():
            job = Job(
                user_id=user_id,
                description="upstream test job",
                action="echo",
                action_params={"message": "hi"},
                job_type="one_shot",
                scheduled_at=scheduled,
            )
            session.add(job)
            await session.flush()

            run = JobRun(
                time_bucket=bucket,
                job_id=job.job_id,
                user_id=job.user_id,
                scheduled_at=scheduled,
                status="SUCCEEDED",
                result=result,
            )
            session.add(run)
            await session.flush()
            return run.run_id


# ---------------------------------------------------------------------------
# Ownership scoping
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_owner_reads_result(session_factory):
    """The run's owner reads their own JobRun.result → Ok(data)."""
    result_data = {"summary": "5 issues closed", "count": 5}
    run_id = await _insert_run(
        session_factory, user_id="victim", result=json.dumps(result_data)
    )

    async with session_factory() as session:
        async with session.begin():
            payload = await read_upstream(run_id, session, user_id="victim")

    assert isinstance(payload, Ok)
    assert payload.data == result_data


@pytest.mark.integration
async def test_cross_tenant_read_returns_noresult(session_factory):
    """A different user reading victim's run_id is blocked → NoResult (not Ok, not error).

    Indistinguishable from a missing run, so the attacker cannot enumerate run_ids.
    """
    result_data = {"secret": "victim-only data"}
    run_id = await _insert_run(
        session_factory, user_id="victim", result=json.dumps(result_data)
    )

    async with session_factory() as session:
        async with session.begin():
            payload = await read_upstream(run_id, session, user_id="attacker")

    assert isinstance(payload, NoResult)
