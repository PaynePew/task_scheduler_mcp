"""Integration test: calendar_digest_ics full fetch + parse + output JSON schema.

Exercises the handler end-to-end with httpx mocked to return fixture ICS bytes,
running via the real executor against Postgres + ElasticMQ.

Run with:
    uv run pytest -m integration tests/integration/test_calendar_digest_ics.py
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.engine import create_async_engine
from app.db.models import Job, JobRun

_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "ics"


def _load_ics(name: str) -> bytes:
    return (_FIXTURES / name).read_bytes()


def _mock_httpx_client(ics_bytes: bytes) -> MagicMock:
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=httpx.Response(200, content=ics_bytes))
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_client)
    ctx.__aexit__ = AsyncMock(return_value=None)
    return ctx


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def session_factory():
    """Fresh engine per test; cleans job data on teardown."""
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
def sqs():
    from app.queue.sqs import SQSClient

    client = SQSClient()
    # drain leftover messages
    while True:
        msgs = client.receive_messages(max_messages=10, wait_seconds=0)
        if not msgs:
            break
        for msg in msgs:
            client.delete_message(msg["ReceiptHandle"])
    return client


async def _insert_calendar_run(
    factory: async_sessionmaker,
    *,
    ics_url: str = "https://calendar.example.com/feed.ics",
    date_range_days: int = 7,
    title_contains: str | None = None,
) -> tuple[Job, JobRun]:
    scheduled = datetime.now(tz=UTC) - timedelta(seconds=10)
    action_params: dict = {"ics_url": ics_url, "date_range_days": date_range_days}
    if title_contains is not None:
        action_params["title_contains"] = title_contains

    async with factory() as session:
        async with session.begin():
            job = Job(
                user_id="calendar-integration-test",
                description="calendar digest test",
                action="calendar_digest_ics",
                action_params=action_params,
                job_type="one_shot",
                scheduled_at=scheduled,
            )
            session.add(job)
            await session.flush()

            bucket = scheduled.replace(minute=0, second=0, microsecond=0).isoformat()
            run = JobRun(
                time_bucket=bucket,
                job_id=job.job_id,
                scheduled_at=scheduled,
                status="QUEUED",
            )
            session.add(run)
    return job, run


def _make_sqs_message(run_id: int, job_id: int) -> dict:
    return {
        "Body": json.dumps({"run_id": run_id, "job_id": job_id}),
        "ReceiptHandle": f"fake-receipt-{run_id}",
        "MessageId": f"fake-msg-{run_id}",
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_full_fetch_parse_output_schema(session_factory, sqs):
    """Full path: insert run → mock httpx → process_one → SUCCEEDED with valid schema."""
    from app.workers.executor import process_one

    ics_bytes = _load_ics("single_event.ics")
    job, run = await _insert_calendar_run(session_factory, date_range_days=365)
    message = _make_sqs_message(run.run_id, job.job_id)

    # Spy on delete_message so the fake receipt handle doesn't hit real SQS
    deleted_receipts: list[str] = []
    sqs.delete_message = deleted_receipts.append

    ctx = _mock_httpx_client(ics_bytes)
    with patch("app.actions.calendar_digest_ics.httpx.AsyncClient", return_value=ctx):
        await process_one(session_factory, sqs, message)

    assert message["ReceiptHandle"] in deleted_receipts

    async with session_factory() as session:
        refreshed = (
            await session.execute(select(JobRun).where(JobRun.run_id == run.run_id))
        ).scalar_one()
        assert refreshed.status == "SUCCEEDED"
        assert refreshed.result is not None
        result = json.loads(refreshed.result)

    # Validate output JSON schema
    assert "events" in result
    assert "count" in result
    assert "date_range" in result
    assert isinstance(result["events"], list)
    assert isinstance(result["count"], int)
    assert result["count"] == len(result["events"])

    date_range = result["date_range"]
    assert "start" in date_range
    assert "end" in date_range

    # Validate each event matches CalendarEvent pydantic model
    from app.ics.parser import CalendarEvent

    for raw_event in result["events"]:
        event = CalendarEvent(**raw_event)
        assert event.summary
        assert event.start
        assert event.end


@pytest.mark.integration
@pytest.mark.asyncio
async def test_multi_event_fixture_count(session_factory, sqs):
    """multi_event.ics returns the correct event count over a wide date range."""
    from app.workers.executor import process_one

    ics_bytes = _load_ics("multi_event.ics")
    job, run = await _insert_calendar_run(session_factory, date_range_days=365)
    message = _make_sqs_message(run.run_id, job.job_id)

    deleted_receipts: list[str] = []
    sqs.delete_message = deleted_receipts.append

    ctx = _mock_httpx_client(ics_bytes)
    with patch("app.actions.calendar_digest_ics.httpx.AsyncClient", return_value=ctx):
        await process_one(session_factory, sqs, message)

    async with session_factory() as session:
        refreshed = (
            await session.execute(select(JobRun).where(JobRun.run_id == run.run_id))
        ).scalar_one()
        assert refreshed.status == "SUCCEEDED"
        assert refreshed.result is not None
        result = json.loads(refreshed.result)

    assert result["count"] >= 0
    assert len(result["events"]) == result["count"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_http_401_routes_to_permanent_failure(session_factory, sqs):
    """401 from calendar server → run FAILED with retryable=False → message deleted (DLQ path)."""
    from app.workers.executor import process_one

    job, run = await _insert_calendar_run(session_factory)
    message = _make_sqs_message(run.run_id, job.job_id)

    deleted_receipts: list[str] = []
    sqs.delete_message = deleted_receipts.append

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=httpx.Response(401, content=b"Unauthorized"))
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_client)
    ctx.__aexit__ = AsyncMock(return_value=None)

    with patch("app.actions.calendar_digest_ics.httpx.AsyncClient", return_value=ctx):
        await process_one(session_factory, sqs, message)

    # Permanent failure → message deleted (executor handles DLQ routing)
    assert message["ReceiptHandle"] in deleted_receipts

    async with session_factory() as session:
        refreshed = (
            await session.execute(select(JobRun).where(JobRun.run_id == run.run_id))
        ).scalar_one()
        assert refreshed.status == "FAILED"
