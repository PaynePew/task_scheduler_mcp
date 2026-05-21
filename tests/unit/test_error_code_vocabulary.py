"""Regression tests for the ADR-060 error-code vocabulary consolidation (issue #157).

Asserts the envelope shape for each surface that previously used a drift code:
  1. OPERATOR_ONLY   → INVALID_STATE  (errors.py / map_domain_error)
  2. OVERLOADED      → INVALID_STATE  (mcp_http.py load-shedding)
  3. OVERLOADED      → INVALID_STATE  (mcp_http.py concurrency cap)
  4. RATE_LIMITED    → INVALID_STATE  (mcp_http.py rate limit)
  5. BACKPRESSURE    → INVALID_STATE  (server.py _handle_task_create)
  6. MISSING_CONNECTION stays as-is   (server.py — formally added to vocabulary)
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import httpx

from app.domain.jobs import OperatorOnlyActionError
from app.entrypoints.mcp_http import build_app
from app.mcp.errors import map_domain_error
from app.mcp.server import _handle_task_create
from app.overload.health import ConcurrencyLimiter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MCP_HEADERS = {
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


def _make_allow_factory():
    """Mock session factory that returns (0, None) → under rate limits → Allow."""
    session = AsyncMock()

    async def _execute(stmt):
        mock_result = MagicMock()
        mock_result.one.return_value = (0, None)
        return mock_result

    session.execute = _execute

    @asynccontextmanager
    async def _factory():
        yield session

    return _factory


# ---------------------------------------------------------------------------
# 1. OPERATOR_ONLY → INVALID_STATE
# ---------------------------------------------------------------------------


def test_operator_only_maps_to_invalid_state():
    """OperatorOnlyActionError must produce INVALID_STATE, not OPERATOR_ONLY."""
    result = map_domain_error(OperatorOnlyActionError("http_call"))
    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_STATE"


# ---------------------------------------------------------------------------
# 2. OVERLOADED → INVALID_STATE  (load-shedding)
# ---------------------------------------------------------------------------


async def test_load_shedding_uses_invalid_state():
    """Load-shedding 503 envelope must use INVALID_STATE (not OVERLOADED)."""
    app = build_app(
        json_response=True,
        session_factory=_make_allow_factory(),
        shed_fn=lambda: True,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/mcp", json=_TASK_CREATE_CALL, headers=_MCP_HEADERS)

    assert resp.status_code == 503
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "INVALID_STATE"


# ---------------------------------------------------------------------------
# 3. OVERLOADED → INVALID_STATE  (concurrency cap)
# ---------------------------------------------------------------------------


async def test_concurrency_cap_uses_invalid_state():
    """Concurrency-cap 503 envelope must use INVALID_STATE (not OVERLOADED)."""
    limiter = ConcurrencyLimiter(limit=1)
    limiter._inflight = 1  # already at capacity

    app = build_app(
        json_response=True,
        session_factory=_make_allow_factory(),
        limiter=limiter,
        shed_fn=lambda: False,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/mcp", json=_TASK_CREATE_CALL, headers=_MCP_HEADERS)

    assert resp.status_code == 503
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "INVALID_STATE"


# ---------------------------------------------------------------------------
# 4. RATE_LIMITED → INVALID_STATE  (HTTP rate-limit middleware)
# ---------------------------------------------------------------------------


async def test_rate_limit_http_uses_invalid_state():
    """Rate-limit 429 envelope must use INVALID_STATE (not RATE_LIMITED)."""
    session = AsyncMock()
    oldest = datetime.now(UTC) - timedelta(seconds=30)

    async def _execute(stmt):
        mock_result = MagicMock()
        # burst_count=9999 exceeds any burst limit; burst_oldest must be a real datetime
        mock_result.one.return_value = (9999, oldest)
        return mock_result

    session.execute = _execute

    @asynccontextmanager
    async def _rl_factory():
        yield session

    app = build_app(
        json_response=True,
        session_factory=_rl_factory,
        shed_fn=lambda: False,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/mcp",
            json=_TASK_CREATE_CALL,
            headers={**_MCP_HEADERS, "X-User-Id": "test-user"},
        )

    assert resp.status_code == 429
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "INVALID_STATE"


# ---------------------------------------------------------------------------
# 5. BACKPRESSURE → INVALID_STATE  (_handle_task_create, _queue_depth injection)
# ---------------------------------------------------------------------------


async def test_backpressure_uses_invalid_state():
    """SQS queue-depth guard must use INVALID_STATE (not BACKPRESSURE)."""
    session = AsyncMock()

    @asynccontextmanager
    async def _factory():
        yield session

    result = await _handle_task_create(
        arguments={"action": "echo", "action_params": {"message": "hi"}},
        user_id="test-user",
        session_factory=_factory,
        _queue_depth=999999,  # vastly exceeds any threshold
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_STATE"
    msg = result["error"]["message"].lower()
    assert "queue" in msg or "busy" in msg


# ---------------------------------------------------------------------------
# 6. MISSING_CONNECTION stays (formally part of the vocabulary now)
# ---------------------------------------------------------------------------


def test_missing_connection_is_in_docstring_vocabulary():
    """map_domain_error docstring must list MISSING_CONNECTION as a valid code."""
    assert "MISSING_CONNECTION" in (map_domain_error.__doc__ or "")
