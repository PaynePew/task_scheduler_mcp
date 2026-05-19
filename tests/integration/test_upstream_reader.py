"""Integration tests for app/chain/upstream_reader.py.

Requires running Postgres (DATABASE_URL set in environment).

Run with:
    uv run pytest -m integration tests/integration/test_upstream_reader.py
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.chain.upstream_reader import (
    InvalidJson,
    NoResult,
    Ok,
    UpstreamError,
    read_upstream,
)
from app.db.engine import create_async_engine
from app.db.models import Job, JobRun

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


async def _insert_run(
    factory: async_sessionmaker,
    *,
    result: str | None = None,
    error_message: str | None = None,
    status: str = "SUCCEEDED",
) -> JobRun:
    """Insert a minimal Job + JobRun and return the persisted JobRun."""
    scheduled = datetime.now(tz=UTC) - timedelta(hours=1)
    bucket = scheduled.replace(minute=0, second=0, microsecond=0).isoformat()

    async with factory() as session:
        async with session.begin():
            job = Job(
                user_id="upstream-reader-test",
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
                scheduled_at=scheduled,
                status=status,
                result=result,
                error_message=error_message,
            )
            session.add(run)
            await session.flush()
            run_id = run.run_id

    # Return a plain object with the run_id for lookups
    async with factory() as session:
        from sqlalchemy import select

        async with session.begin():
            refreshed = (
                await session.execute(select(JobRun).where(JobRun.run_id == run_id))
            ).scalar_one()
            return refreshed


# ---------------------------------------------------------------------------
# Ok — completed upstream run with valid JSON result
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_ok_when_upstream_run_has_json_result(session_factory):
    """Completed upstream JobRun with valid JSON result → Ok(parsed_json)."""
    payload_data = {"summary": "5 issues closed", "count": 5}
    run = await _insert_run(factory=session_factory, result=json.dumps(payload_data))

    async with session_factory() as session:
        async with session.begin():
            payload = await read_upstream(run.run_id, session)

    assert isinstance(payload, Ok)
    assert payload.data == payload_data


# ---------------------------------------------------------------------------
# NoResult — upstream run has result=NULL
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_no_result_when_upstream_result_null(session_factory):
    """Upstream JobRun with result=NULL → NoResult."""
    run = await _insert_run(factory=session_factory, result=None, error_message=None)

    async with session_factory() as session:
        async with session.begin():
            payload = await read_upstream(run.run_id, session)

    assert isinstance(payload, NoResult)


# ---------------------------------------------------------------------------
# InvalidJson — upstream run has non-JSON result
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_invalid_json_when_upstream_result_is_malformed(session_factory):
    """Upstream JobRun with invalid JSON in result → InvalidJson."""
    raw = "not-valid-json{{"
    run = await _insert_run(factory=session_factory, result=raw)

    async with session_factory() as session:
        async with session.begin():
            payload = await read_upstream(run.run_id, session)

    assert isinstance(payload, InvalidJson)
    assert payload.raw == raw


# ---------------------------------------------------------------------------
# NoResult — missing run_id (run does not exist)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_no_result_when_run_id_missing(session_factory):
    """Non-existent run_id → NoResult."""
    async with session_factory() as session:
        async with session.begin():
            payload = await read_upstream(999_999_999, session)

    assert isinstance(payload, NoResult)


# ---------------------------------------------------------------------------
# UpstreamError — result=NULL but error_message is set (failed run)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_upstream_error_when_result_null_error_message_set(session_factory):
    """Upstream JobRun with result=NULL and error_message set → UpstreamError."""
    msg = "http 500 from downstream API"
    run = await _insert_run(
        factory=session_factory,
        result=None,
        error_message=msg,
        status="FAILED",
    )

    async with session_factory() as session:
        async with session.begin():
            payload = await read_upstream(run.run_id, session)

    assert isinstance(payload, UpstreamError)
    assert payload.error_msg == msg
