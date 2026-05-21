"""Unit tests for HTTP-layer overload protection in mcp_http.py (ADR-057).

Tests load shedding (503), concurrency limiting (503), and the /healthz/shed
endpoint — all external calls (DB, psutil, SQS) are mocked or injected.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import httpx

from app.entrypoints.mcp_http import build_app
from app.overload.health import ConcurrencyLimiter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_factory(*, raise_exc: Exception | None = None):
    """Return a mock async_sessionmaker-compatible factory (no real DB)."""
    session = AsyncMock()
    if raise_exc is not None:
        session.execute = AsyncMock(side_effect=raise_exc)
    else:
        # Stub out rate-limit DB query: return (0, None) → under burst + daily limits
        from unittest.mock import MagicMock

        async def _execute(stmt):
            mock_result = MagicMock()
            mock_result.one.return_value = (0, None)
            return mock_result

        session.execute = _execute

    @asynccontextmanager
    async def _factory():
        yield session

    return _factory


MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
}

_TASK_CREATE_CALL = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {
        "name": "task.create.v1",
        "arguments": {"action": "echo", "action_params": {"message": "hi"}},
    },
}


# ---------------------------------------------------------------------------
# Load shedding — 503 + Retry-After
# ---------------------------------------------------------------------------


async def test_load_shedding_returns_503_with_retry_after():
    """When shed_fn returns True, POST /mcp returns 503 + Retry-After header."""
    app = build_app(
        json_response=True,
        session_factory=_make_factory(),
        shed_fn=lambda: True,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/mcp", json=_TASK_CREATE_CALL, headers=MCP_HEADERS)

    assert resp.status_code == 503
    assert "Retry-After" in resp.headers
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "INVALID_STATE"


async def test_load_shedding_not_triggered_when_healthy():
    """When shed_fn returns False, request proceeds past the shed check."""
    app = build_app(
        json_response=True,
        session_factory=_make_factory(),
        shed_fn=lambda: False,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/mcp", json=_TASK_CREATE_CALL, headers=MCP_HEADERS)

    # Rate limit check runs (DB returns (0, None) → Allow), then MCP handler runs.
    # The MCP handler itself will fail (no real DB), but HTTP status won't be 503.
    assert resp.status_code != 503


# ---------------------------------------------------------------------------
# Concurrency limiting — 503
# ---------------------------------------------------------------------------


async def test_concurrency_limiter_rejects_at_capacity():
    """A limiter already at capacity returns 503 for the next request."""
    limiter = ConcurrencyLimiter(limit=1)
    # Manually mark one slot as in-use so the next acquire raises CapacityExceeded.
    limiter._inflight = 1

    app = build_app(
        json_response=True,
        session_factory=_make_factory(),
        limiter=limiter,
        shed_fn=lambda: False,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/mcp", json=_TASK_CREATE_CALL, headers=MCP_HEADERS)

    assert resp.status_code == 503
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "INVALID_STATE"


async def test_concurrency_limiter_allows_when_under_capacity():
    """Limiter with free slots does not block the request."""
    limiter = ConcurrencyLimiter(limit=5)  # plenty of room

    app = build_app(
        json_response=True,
        session_factory=_make_factory(),
        limiter=limiter,
        shed_fn=lambda: False,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/mcp", json=_TASK_CREATE_CALL, headers=MCP_HEADERS)

    assert resp.status_code != 503


# ---------------------------------------------------------------------------
# /healthz/shed endpoint
# ---------------------------------------------------------------------------


async def test_shed_endpoint_returns_200_when_healthy():
    """/healthz/shed returns 200 {"ok": true, "shed": false} when not shedding."""
    app = build_app(
        json_response=True,
        session_factory=_make_factory(),
        shed_fn=lambda: False,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/healthz/shed")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["shed"] is False


async def test_shed_endpoint_returns_503_when_shedding():
    """/healthz/shed returns 503 {"ok": false, "shed": true} when overloaded."""
    app = build_app(
        json_response=True,
        session_factory=_make_factory(),
        shed_fn=lambda: True,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/healthz/shed")

    assert resp.status_code == 503
    assert "Retry-After" in resp.headers
    body = resp.json()
    assert body["ok"] is False
    assert body["shed"] is True
