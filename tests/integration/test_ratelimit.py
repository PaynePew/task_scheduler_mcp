"""Integration tests for Postgres-backed rate limiting (ADR-042/ADR-057).

Rate limiting now lives in the HTTP middleware (_McpHttpEndpoint) and returns
HTTP 429 + Retry-After rather than a bare MCP envelope error.  Handler-level
tests exercise the full HTTP stack via httpx.ASGITransport.

Requires a live Postgres instance with schema applied.
Run: uv run pytest -m integration tests/integration/test_ratelimit.py
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.engine import create_async_engine
from app.entrypoints.mcp_http import build_app
from app.mcp.server import _handle_task_create
from app.ratelimit.checker import Allow, RateLimits, Reject, check_rate_limit


@pytest_asyncio.fixture
async def session_factory():
    """Fresh engine per test — avoids cross-event-loop pool reuse."""
    engine = create_async_engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    async with factory() as session:
        async with session.begin():
            await session.execute(text("DELETE FROM run_events"))
            await session.execute(text("DELETE FROM job_runs"))
            await session.execute(text("DELETE FROM jobs"))
    await engine.dispose()


async def _bulk_insert_jobs(
    factory: async_sessionmaker,
    user_id: str,
    count: int,
    created_at: datetime,
) -> None:
    """Insert *count* minimal job rows with a fixed created_at (bypasses ORM defaults)."""
    async with factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "INSERT INTO jobs "
                    "(user_id, description, action, action_params, job_type, "
                    "scheduled_at, timezone, active, created_at, updated_at) "
                    "SELECT :user_id, 'rate-limit-seed', 'echo', '{}', 'one_shot', "
                    "now() + interval '1 hour', 'UTC', true, :created_at, :created_at "
                    "FROM generate_series(1, :count)"
                ),
                {"user_id": user_id, "created_at": created_at, "count": count},
            )


# ---------------------------------------------------------------------------
# Direct checker tests (bypass handler for speed)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_checker_allow_under_limits(session_factory):
    """No jobs seeded → checker returns Allow."""
    limits = RateLimits(daily=1000, burst=10)
    async with session_factory() as session:
        decision = await check_rate_limit("rl-user-allow", session, limits)
    assert isinstance(decision, Allow)


@pytest.mark.integration
async def test_checker_reject_daily_at_1000(session_factory):
    """1000 jobs within 24 h → next check returns Reject(daily)."""
    user_id = "rl-user-daily"
    now = datetime.now(UTC)
    within_window = now - timedelta(hours=1)
    await _bulk_insert_jobs(session_factory, user_id, 1000, within_window)

    limits = RateLimits(daily=1000, burst=10)
    async with session_factory() as session:
        decision = await check_rate_limit(user_id, session, limits)

    assert isinstance(decision, Reject)
    assert decision.reason == "daily"
    assert decision.retry_after_seconds > 0


@pytest.mark.integration
async def test_checker_reject_burst_at_10(session_factory):
    """10 jobs within 1 minute → next check returns Reject(burst)."""
    user_id = "rl-user-burst"
    now = datetime.now(UTC)
    within_burst = now - timedelta(seconds=30)
    await _bulk_insert_jobs(session_factory, user_id, 10, within_burst)

    limits = RateLimits(daily=1000, burst=10)
    async with session_factory() as session:
        decision = await check_rate_limit(user_id, session, limits)

    assert isinstance(decision, Reject)
    assert decision.reason == "burst"
    assert decision.retry_after_seconds > 0


@pytest.mark.integration
async def test_checker_daily_window_reset(session_factory):
    """Jobs created >24 h ago don't count towards the daily window."""
    user_id = "rl-user-reset"
    now = datetime.now(UTC)
    outside_window = now - timedelta(hours=25)
    await _bulk_insert_jobs(session_factory, user_id, 1000, outside_window)

    limits = RateLimits(daily=1000, burst=10)
    async with session_factory() as session:
        decision = await check_rate_limit(user_id, session, limits)

    assert isinstance(decision, Allow)


@pytest.mark.integration
async def test_checker_burst_window_reset(session_factory):
    """Jobs created >1 minute ago don't count towards the burst window."""
    user_id = "rl-user-burst-reset"
    now = datetime.now(UTC)
    outside_burst = now - timedelta(seconds=61)
    await _bulk_insert_jobs(session_factory, user_id, 10, outside_burst)

    limits = RateLimits(daily=1000, burst=10)
    async with session_factory() as session:
        decision = await check_rate_limit(user_id, session, limits)

    assert isinstance(decision, Allow)


@pytest.mark.integration
async def test_checker_multi_user_isolation(session_factory):
    """User A at daily limit doesn't affect user B."""
    now = datetime.now(UTC)
    within_window = now - timedelta(hours=1)
    await _bulk_insert_jobs(session_factory, "rl-iso-user-a", 1000, within_window)

    limits = RateLimits(daily=1000, burst=10)

    async with session_factory() as session:
        decision_a = await check_rate_limit("rl-iso-user-a", session, limits)
    assert isinstance(decision_a, Reject)
    assert decision_a.reason == "daily"

    async with session_factory() as session:
        decision_b = await check_rate_limit("rl-iso-user-b", session, limits)
    assert isinstance(decision_b, Allow)


# ---------------------------------------------------------------------------
# HTTP-level rate-limit tests (ADR-057: 429 + Retry-After from middleware)
# ---------------------------------------------------------------------------

MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
}


def _create_call() -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "task.create.v1",
            "arguments": {"action": "echo", "action_params": {"message": "hi"}},
        },
    }


@pytest.mark.integration
async def test_http_1001st_job_returns_429(session_factory):
    """1000 jobs in 24 h → 1001st POST /mcp returns HTTP 429 + Retry-After header."""
    user_id = "rl-http-daily"
    now = datetime.now(UTC)
    within_window = now - timedelta(hours=1)
    await _bulk_insert_jobs(session_factory, user_id, 1000, within_window)

    app = build_app(
        json_response=True,
        session_factory=session_factory,
        shed_fn=lambda: False,  # disable load shedding in test
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/mcp",
            json=_create_call(),
            headers={**MCP_HEADERS, "X-User-Id": user_id},
        )

    assert resp.status_code == 429
    assert "Retry-After" in resp.headers
    assert int(resp.headers["Retry-After"]) > 0
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "INVALID_STATE"
    assert "daily" in body["error"]["message"]
    assert body["error"]["retry_after_seconds"] > 0


@pytest.mark.integration
async def test_http_11th_burst_job_returns_429(session_factory):
    """10 jobs within 1 minute → 11th POST /mcp returns HTTP 429 + Retry-After header."""
    user_id = "rl-http-burst"
    now = datetime.now(UTC)
    within_burst = now - timedelta(seconds=30)
    await _bulk_insert_jobs(session_factory, user_id, 10, within_burst)

    app = build_app(
        json_response=True,
        session_factory=session_factory,
        shed_fn=lambda: False,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/mcp",
            json=_create_call(),
            headers={**MCP_HEADERS, "X-User-Id": user_id},
        )

    assert resp.status_code == 429
    assert "Retry-After" in resp.headers
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "INVALID_STATE"
    assert "burst" in body["error"]["message"]


@pytest.mark.integration
async def test_http_under_limit_succeeds(session_factory):
    """HTTP request under both rate limits returns 200 with job_id."""
    user_id = "rl-http-ok"

    app = build_app(
        json_response=True,
        session_factory=session_factory,
        shed_fn=lambda: False,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/mcp",
            json=_create_call(),
            headers={**MCP_HEADERS, "X-User-Id": user_id},
        )

    assert resp.status_code == 200
    body = resp.json()
    result_text = body["result"]["content"][0]["text"]
    import json

    result = json.loads(result_text)
    assert result["ok"] is True
    assert "job_id" in result["data"]


# ---------------------------------------------------------------------------
# Handler-level smoke test (no rate-limit check; verifies create_job still works)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_handler_under_limit_succeeds(session_factory):
    """_handle_task_create succeeds when user is under limits (no rate-limit in handler)."""
    result = await _handle_task_create(
        arguments={"action": "echo", "action_params": {"message": "ok"}},
        user_id="rl-handler-ok",
        session_factory=session_factory,
        _queue_depth=0,
    )

    assert result["ok"] is True
    assert "job_id" in result["data"]
