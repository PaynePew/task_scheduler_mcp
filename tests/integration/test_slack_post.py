"""Integration tests for slack_post handler — require running Postgres.

Tests the full stack:
  - OAuth token resolved via patched get_token
  - from_run_id reading a real upstream JobRun.result from Postgres
  - UpstreamPayload variants (Ok, NoResult, InvalidJson) via committed rows

Run with:
    uv run pytest -m integration tests/integration/test_slack_post.py
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.actions.slack_post import SlackPostHandler, SlackPostParams, SlackTemplate
from app.chain.upstream_reader import InvalidJson, NoResult, Ok, UpstreamError, read_upstream
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
) -> tuple[Job, JobRun]:
    """Insert a Job + JobRun with the given result, committed to the DB."""
    scheduled = datetime.now(tz=UTC) - timedelta(seconds=10)
    async with factory() as session:
        async with session.begin():
            job = Job(
                user_id="slack-post-test",
                description="upstream test job",
                action="echo",
                action_params={"message": "upstream"},
                job_type="one_shot",
                scheduled_at=scheduled,
            )
            session.add(job)
            await session.flush()

            bucket = scheduled.replace(minute=0, second=0, microsecond=0).isoformat()
            run = JobRun(
                time_bucket=bucket,
                job_id=job.job_id,
                user_id=job.user_id,
                scheduled_at=scheduled,
                status=status,
                result=result,
                error_message=error_message,
            )
            session.add(run)
            await session.flush()
            run_id = run.run_id
            job_id = job.job_id

    # Re-fetch in a separate session to get the committed row with run_id populated.
    async with factory() as session:
        async with session.begin():
            fetched_job = await session.get(Job, job_id)
            fetched_run = (
                await session.execute(select(JobRun).where(JobRun.run_id == run_id))
            ).scalar_one()
    return fetched_job, fetched_run


def _mock_slack_post(ok: bool = True) -> tuple[MagicMock, list[dict]]:
    """Return (patched AsyncClient ctx, captured_payloads list)."""
    captured: list[dict] = []

    async def fake_post(url: str, **kwargs: object) -> httpx.Response:
        captured.append(kwargs.get("json", {}))
        body = {"ok": ok}
        if ok:
            body["channel"] = "C12345"
            body["ts"] = "1234567890.123456"
        else:
            body["error"] = "channel_not_found"
        return httpx.Response(200, content=json.dumps(body).encode())

    mock_client = AsyncMock()
    mock_client.post = fake_post
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_client)
    ctx.__aexit__ = AsyncMock(return_value=None)
    return ctx, captured


# ---------------------------------------------------------------------------
# Integration tests: upstream_reader against real DB
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_upstream_reader_ok_variant(session_factory):
    """Committed run with valid JSON result → read_upstream returns Ok."""
    result_data = {"key": "value", "count": 42}
    _, run = await _insert_run(session_factory, result=json.dumps(result_data))

    async with session_factory() as session:
        async with session.begin():
            payload = await read_upstream(run_id=run.run_id, session=session)

    assert isinstance(payload, Ok)
    assert payload.data == result_data


@pytest.mark.integration
async def test_upstream_reader_no_result_null(session_factory):
    """Committed run with result=NULL → read_upstream returns NoResult."""
    _, run = await _insert_run(session_factory, result=None)

    async with session_factory() as session:
        async with session.begin():
            payload = await read_upstream(run_id=run.run_id, session=session)

    assert isinstance(payload, NoResult)


@pytest.mark.integration
async def test_upstream_reader_invalid_json(session_factory):
    """Committed run with non-JSON result → read_upstream returns InvalidJson."""
    _, run = await _insert_run(session_factory, result="not-json-{]")

    async with session_factory() as session:
        async with session.begin():
            payload = await read_upstream(run_id=run.run_id, session=session)

    assert isinstance(payload, InvalidJson)
    assert payload.raw == "not-json-{]"


@pytest.mark.integration
async def test_upstream_reader_missing_run_id(session_factory):
    """Non-existent run_id → read_upstream returns NoResult."""
    async with session_factory() as session:
        async with session.begin():
            payload = await read_upstream(run_id=999_999_999, session=session)

    assert isinstance(payload, NoResult)


@pytest.mark.integration
async def test_upstream_reader_error_message(session_factory):
    """Run with error_message and no result → UpstreamError."""
    _, run = await _insert_run(
        session_factory, result=None, error_message="upstream failed", status="FAILED"
    )

    async with session_factory() as session:
        async with session.begin():
            payload = await read_upstream(run_id=run.run_id, session=session)

    assert isinstance(payload, UpstreamError)
    assert "upstream failed" in payload.error_msg


# ---------------------------------------------------------------------------
# Integration tests: full slack_post execute() with real upstream DB read
# ---------------------------------------------------------------------------


@dataclass
class FakeRun:
    user_id: str = "slack-post-test"


@pytest.mark.integration
async def test_slack_post_from_run_id_ok_variant(session_factory):
    """from_run_id with upstream Ok → formats message and POSTs to Slack."""
    result_data = {"issues_closed": 3, "prs_merged": 1}
    _, upstream_run = await _insert_run(session_factory, result=json.dumps(result_data))

    ctx, captured = _mock_slack_post(ok=True)
    handler = SlackPostHandler()

    params = SlackPostParams(
        channel="#general",
        from_run_id=upstream_run.run_id,
        template=SlackTemplate.digest_v1,
    )

    # Patch the module-level engine's session factory so _message_from_upstream
    # uses the per-test engine instead of the shared module-level singleton.
    with (
        patch("app.actions.slack_post.get_token", AsyncMock(return_value="xoxb-fake-token")),
        patch("app.db.engine.async_session_factory", session_factory),
        patch("app.actions.slack_post.httpx.AsyncClient", return_value=ctx),
    ):
        result = await handler.execute(run=FakeRun(), params=params)

    assert result.ok is True
    assert captured
    text = captured[0]["text"]
    assert "*Daily Digest*" in text
    assert "issues_closed" in text


@pytest.mark.integration
async def test_slack_post_from_run_id_error_variant(session_factory):
    """from_run_id with upstream error → formats ⚠ alert message."""
    _, upstream_run = await _insert_run(
        session_factory, result=None, error_message="github API rate limited", status="FAILED"
    )

    ctx, captured = _mock_slack_post(ok=True)
    handler = SlackPostHandler()

    params = SlackPostParams(
        channel="#alerts",
        from_run_id=upstream_run.run_id,
        template=SlackTemplate.digest_v1,
    )

    # Patch the module-level engine's session factory so _message_from_upstream
    # uses the per-test engine instead of the shared module-level singleton.
    with (
        patch("app.actions.slack_post.get_token", AsyncMock(return_value="xoxb-fake-token")),
        patch("app.db.engine.async_session_factory", session_factory),
        patch("app.actions.slack_post.httpx.AsyncClient", return_value=ctx),
    ):
        result = await handler.execute(run=FakeRun(), params=params)

    assert result.ok is True
    assert captured
    text = captured[0]["text"]
    assert "⚠" in text
    assert "Digest unavailable" in text
