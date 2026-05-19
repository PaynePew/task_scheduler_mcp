"""Unit tests for the http_call action handler (no real network)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.actions.http_call import MAX_RESPONSE_BYTES, HttpCallHandler, HttpCallParams
from app.actions.registry import ACTION_REGISTRY


def _mock_client(response: httpx.Response) -> MagicMock:
    """Build a mock AsyncClient whose ``request`` coroutine returns *response*."""
    mock_client = AsyncMock()
    mock_client.request = AsyncMock(return_value=response)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_client)
    ctx.__aexit__ = AsyncMock(return_value=None)
    return ctx


def _patch_client(response: httpx.Response):
    ctx = _mock_client(response)
    return patch("app.actions.http_call.httpx.AsyncClient", return_value=ctx)


@pytest.mark.asyncio
async def test_http_call_200_ok():
    handler = HttpCallHandler()
    params = HttpCallParams(method="GET", url="https://example.com")
    response = httpx.Response(200, content=b"hello")

    with _patch_client(response):
        result = await handler.execute(run=None, params=params)

    assert result.ok is True
    assert result.retryable is False
    assert result.error is None
    assert result.result is not None
    assert result.result["status_code"] == 200
    assert result.result["body"] == "hello"


@pytest.mark.asyncio
async def test_http_call_500_retryable():
    handler = HttpCallHandler()
    params = HttpCallParams(method="GET", url="https://example.com")
    response = httpx.Response(500, content=b"server error")

    with _patch_client(response):
        result = await handler.execute(run=None, params=params)

    assert result.ok is False
    assert result.retryable is True
    assert "500" in (result.error or "")


@pytest.mark.asyncio
async def test_http_call_404_not_retryable():
    handler = HttpCallHandler()
    params = HttpCallParams(method="GET", url="https://example.com")
    response = httpx.Response(404, content=b"not found")

    with _patch_client(response):
        result = await handler.execute(run=None, params=params)

    assert result.ok is False
    assert result.retryable is False
    assert "404" in (result.error or "")


@pytest.mark.asyncio
async def test_http_call_large_body_truncated_to_2kb():
    handler = HttpCallHandler()
    params = HttpCallParams(method="GET", url="https://example.com")
    large_body = b"x" * 10_000  # 10 KB — well over the 2 KB limit
    response = httpx.Response(200, content=large_body)

    with _patch_client(response):
        result = await handler.execute(run=None, params=params)

    assert result.ok is True
    assert result.result is not None
    assert len(result.result["body"].encode("utf-8")) <= MAX_RESPONSE_BYTES


@pytest.mark.asyncio
async def test_http_call_network_error_retryable():
    handler = HttpCallHandler()
    params = HttpCallParams(method="GET", url="https://example.com")

    mock_client = AsyncMock()
    mock_client.request = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_client)
    ctx.__aexit__ = AsyncMock(return_value=None)

    with patch("app.actions.http_call.httpx.AsyncClient", return_value=ctx):
        result = await handler.execute(run=None, params=params)

    assert result.ok is False
    assert result.retryable is True


def test_http_call_registered_in_registry():
    assert "http_call" in ACTION_REGISTRY
    assert isinstance(ACTION_REGISTRY["http_call"], HttpCallHandler)


def test_list_actions_exposes_both_echo_and_http_call():
    assert "echo" in ACTION_REGISTRY
    assert "http_call" in ACTION_REGISTRY


# ---------------------------------------------------------------------------
# ${VAR} substitution via secrets resolver
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_call_var_substituted_in_header():
    """${ANTHROPIC_API_KEY} in headers is resolved to the env value before the request."""
    handler = HttpCallHandler()
    params = HttpCallParams(
        method="GET",
        url="https://example.com",
        headers={"Authorization": "Bearer ${ANTHROPIC_API_KEY}"},
    )
    response = httpx.Response(200, content=b"ok")

    captured: dict = {}

    async def _fake_request(**kwargs):
        captured["headers"] = kwargs.get("headers", {})
        return response

    mock_client = AsyncMock()
    mock_client.request = _fake_request
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_client)
    ctx.__aexit__ = AsyncMock(return_value=None)

    with (
        patch("app.actions.http_call.httpx.AsyncClient", return_value=ctx),
        patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-ant-real-key"}),
    ):
        result = await handler.execute(run=None, params=params)

    assert result.ok is True
    assert captured["headers"].get("Authorization") == "Bearer sk-ant-real-key"


@pytest.mark.asyncio
async def test_http_call_non_whitelisted_var_rejected():
    """A ${VAR} not in the whitelist causes a permanent failure (retryable=False)."""
    handler = HttpCallHandler()
    params = HttpCallParams(
        method="GET",
        url="https://example.com",
        headers={"X-Secret": "${DATABASE_PASSWORD}"},
    )

    with patch.dict("os.environ", {"DATABASE_PASSWORD": "hunter2"}):
        result = await handler.execute(run=None, params=params)

    assert result.ok is False
    assert result.retryable is False
    assert "DATABASE_PASSWORD" in (result.error or "")


@pytest.mark.asyncio
async def test_http_call_no_var_syntax_unchanged():
    """Plain headers without ${...} are passed through without modification."""
    handler = HttpCallHandler()
    params = HttpCallParams(
        method="GET",
        url="https://example.com",
        headers={"Content-Type": "application/json"},
    )
    response = httpx.Response(200, content=b"ok")

    with _patch_client(response):
        result = await handler.execute(run=None, params=params)

    assert result.ok is True
