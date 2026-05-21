"""Unit tests for the slack_post action handler (no real network or DB)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.actions.base import CredentialMode
from app.actions.registry import ACTION_REGISTRY
from app.actions.slack_post import (
    SlackPostHandler,
    SlackPostParams,
    SlackTemplate,
    _format_digest_v1,
    _format_interview_brief,
    _format_raw,
)
from app.chain.upstream_reader import InvalidJson, NoResult, Ok, UpstreamError
from app.connections.store import ConnectionMiss

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeRun:
    user_id: str = "user-test"


def _make_slack_response(ok: bool = True, error: str | None = None) -> httpx.Response:
    """Build a Slack-style JSON response (HTTP 200 with ok: bool)."""
    body: dict[str, Any] = {"ok": ok}
    if ok:
        body["channel"] = "C12345"
        body["ts"] = "1234567890.123456"
    else:
        body["error"] = error or "unknown"
    import json as _json

    return httpx.Response(200, content=_json.dumps(body).encode())


def _patch_http_slack(response: httpx.Response | None = None):
    if response is None:
        response = _make_slack_response(ok=True)
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=response)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_client)
    ctx.__aexit__ = AsyncMock(return_value=None)
    return patch("app.actions.slack_post.httpx.AsyncClient", return_value=ctx)


def _capture_http_post(response: httpx.Response | None = None) -> tuple[MagicMock, list[dict]]:
    """Return (AsyncClient ctx, captured_payloads list) — appends each POST JSON body."""
    if response is None:
        response = _make_slack_response(ok=True)
    captured: list[dict] = []

    async def fake_post(url: str, **kwargs: object) -> httpx.Response:
        captured.append(kwargs.get("json", {}))
        return response

    mock_client = AsyncMock()
    mock_client.post = fake_post
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_client)
    ctx.__aexit__ = AsyncMock(return_value=None)
    return ctx, captured


# ---------------------------------------------------------------------------
# Template formatter unit tests
# ---------------------------------------------------------------------------


class TestRawTemplate:
    def test_ok_dict(self):
        result = _format_raw({"key": "val"}, is_error=False)
        assert "key" in result

    def test_ok_string(self):
        assert _format_raw("hello", is_error=False) == "hello"

    def test_error_path_prepends_warning(self):
        result = _format_raw("boom", is_error=True)
        assert "⚠" in result
        assert "boom" in result


class TestDigestV1Template:
    def test_ok_dict_has_header(self):
        result = _format_digest_v1({"issues": 5}, is_error=False)
        assert "*Daily Digest*" in result
        assert "issues" in result
        assert "5" in result

    def test_ok_non_dict_fallback(self):
        result = _format_digest_v1("plain text", is_error=False)
        assert "*Daily Digest*" in result

    def test_error_path(self):
        result = _format_digest_v1("upstream boom", is_error=True)
        assert "⚠" in result
        assert "Digest unavailable" in result


class TestInterviewBriefTemplate:
    def test_ok_dict(self):
        result = _format_interview_brief({"role": "engineer"}, is_error=False)
        assert "*Interview Brief*" in result
        assert "role" in result

    def test_ok_non_dict_fallback(self):
        result = _format_interview_brief("plain", is_error=False)
        assert "*Interview Brief*" in result

    def test_error_path(self):
        result = _format_interview_brief("error msg", is_error=True)
        assert "⚠" in result
        assert "Interview brief unavailable" in result


# ---------------------------------------------------------------------------
# Handler metadata
# ---------------------------------------------------------------------------


def test_handler_credential_mode_is_oauth_connection():
    assert SlackPostHandler.credential_mode == CredentialMode.oauth_connection


def test_handler_required_provider_is_slack():
    assert SlackPostHandler.required_provider == "slack"


def test_handler_not_operator_only():
    assert SlackPostHandler.requires_operator is False


# ---------------------------------------------------------------------------
# SlackPostHandler — ConnectionMiss → error with connect_url
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slack_post_connection_miss_returns_connect_url():
    handler = SlackPostHandler()
    params = SlackPostParams(channel="#general", message="hello")
    run = FakeRun()

    with patch(
        "app.actions.slack_post.get_token",
        AsyncMock(side_effect=ConnectionMiss("user-test", "slack")),
    ):
        result = await handler.execute(run=run, params=params)

    assert result.ok is False
    assert result.retryable is False
    assert "connect" in (result.error or "").lower() or "/connections" in (result.error or "")


# ---------------------------------------------------------------------------
# SlackPostHandler — happy path POST via chat.postMessage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slack_post_200_ok():
    handler = SlackPostHandler()
    params = SlackPostParams(channel="#general", message="hello")
    run = FakeRun()

    with (
        patch("app.actions.slack_post.get_token", AsyncMock(return_value="xoxb-fake-token")),
        _patch_http_slack(_make_slack_response(ok=True)),
    ):
        result = await handler.execute(run=run, params=params)

    assert result.ok is True
    assert result.retryable is False
    assert result.error is None
    assert result.result is not None
    assert result.result["channel"] == "C12345"


@pytest.mark.asyncio
async def test_slack_post_uses_bearer_token():
    """chat.postMessage is called with Authorization: Bearer <token>."""
    handler = SlackPostHandler()
    params = SlackPostParams(channel="#c", message="hi")
    run = FakeRun()

    captured_headers: list[dict] = []

    async def fake_post(url: str, **kwargs: object) -> httpx.Response:
        captured_headers.append(dict(kwargs.get("headers", {})))
        return _make_slack_response(ok=True)

    mock_client = AsyncMock()
    mock_client.post = fake_post
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_client)
    ctx.__aexit__ = AsyncMock(return_value=None)

    with (
        patch("app.actions.slack_post.get_token", AsyncMock(return_value="xoxb-my-real-token")),
        patch("app.actions.slack_post.httpx.AsyncClient", return_value=ctx),
    ):
        result = await handler.execute(run=run, params=params)

    assert result.ok is True
    assert captured_headers
    assert captured_headers[0].get("Authorization") == "Bearer xoxb-my-real-token"


# ---------------------------------------------------------------------------
# Error classification — HTTP-level errors
# ---------------------------------------------------------------------------


def test_slack_post_http_429_is_retryable():
    result = SlackPostHandler._classify_response(httpx.Response(429, content=b"rate limited"))
    assert result.ok is False
    assert result.retryable is True
    assert "429" in (result.error or "")


def test_slack_post_http_500_is_retryable():
    result = SlackPostHandler._classify_response(httpx.Response(500, content=b"error"))
    assert result.ok is False
    assert result.retryable is True


# ---------------------------------------------------------------------------
# Error classification — Slack JSON-level errors (HTTP 200)
# ---------------------------------------------------------------------------


def test_classify_response_ok_true():
    import json

    body = {"ok": True, "channel": "C1", "ts": "123.456"}
    resp = httpx.Response(200, content=json.dumps(body).encode())
    result = SlackPostHandler._classify_response(resp)
    assert result.ok is True
    assert result.result["channel"] == "C1"
    assert result.result["ts"] == "123.456"


def test_classify_response_invalid_auth_not_retryable():
    resp = _make_slack_response(ok=False, error="invalid_auth")
    result = SlackPostHandler._classify_response(resp)
    assert result.ok is False
    assert result.retryable is False
    assert "invalid_auth" in (result.error or "")


def test_classify_response_token_revoked_not_retryable():
    result = SlackPostHandler._classify_response(
        _make_slack_response(ok=False, error="token_revoked")
    )
    assert result.ok is False
    assert result.retryable is False


def test_classify_response_channel_not_found_not_retryable():
    result = SlackPostHandler._classify_response(
        _make_slack_response(ok=False, error="channel_not_found")
    )
    assert result.ok is False
    assert result.retryable is False


def test_classify_response_ratelimited_is_retryable():
    result = SlackPostHandler._classify_response(
        _make_slack_response(ok=False, error="ratelimited")
    )
    assert result.ok is False
    assert result.retryable is True


def test_classify_response_non_json_not_retryable():
    resp = httpx.Response(200, content=b"not-json-!!")
    result = SlackPostHandler._classify_response(resp)
    assert result.ok is False
    assert result.retryable is False


# ---------------------------------------------------------------------------
# Network / timeout errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slack_post_timeout_is_retryable():
    handler = SlackPostHandler()
    params = SlackPostParams(channel="#c", message="m")
    run = FakeRun()

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_client)
    ctx.__aexit__ = AsyncMock(return_value=None)

    with (
        patch("app.actions.slack_post.get_token", AsyncMock(return_value="xoxb-fake-token")),
        patch("app.actions.slack_post.httpx.AsyncClient", return_value=ctx),
    ):
        result = await handler.execute(run=run, params=params)

    assert result.ok is False
    assert result.retryable is True
    assert "Timeout" in (result.error or "")


@pytest.mark.asyncio
async def test_slack_post_network_error_is_retryable():
    handler = SlackPostHandler()
    params = SlackPostParams(channel="#c", message="m")
    run = FakeRun()

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_client)
    ctx.__aexit__ = AsyncMock(return_value=None)

    with (
        patch("app.actions.slack_post.get_token", AsyncMock(return_value="xoxb-fake-token")),
        patch("app.actions.slack_post.httpx.AsyncClient", return_value=ctx),
    ):
        result = await handler.execute(run=run, params=params)

    assert result.ok is False
    assert result.retryable is True


# ---------------------------------------------------------------------------
# from_run_id — upstream variants via mocked read_upstream
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_from_run_id_ok_raw_template():
    data = {"summary": "5 issues closed"}

    with (
        patch("app.actions.slack_post.get_token", AsyncMock(return_value="xoxb-fake-token")),
        patch("app.actions.slack_post.read_upstream", AsyncMock(return_value=Ok(data=data))),
        _patch_http_slack(),
    ):
        handler = SlackPostHandler()
        params = SlackPostParams(channel="#c", from_run_id=42, template=SlackTemplate.raw)
        result = await handler.execute(run=FakeRun(), params=params)

    assert result.ok is True


@pytest.mark.asyncio
async def test_from_run_id_upstream_error_raw_template():
    payload = UpstreamError(error_msg="upstream failed")

    with (
        patch("app.actions.slack_post.get_token", AsyncMock(return_value="xoxb-fake-token")),
        patch("app.actions.slack_post.read_upstream", AsyncMock(return_value=payload)),
        _patch_http_slack(),
    ):
        handler = SlackPostHandler()
        params = SlackPostParams(channel="#c", from_run_id=42, template=SlackTemplate.raw)
        result = await handler.execute(run=FakeRun(), params=params)

    assert result.ok is True  # POST succeeded even with error-path formatting


@pytest.mark.asyncio
async def test_from_run_id_no_result_digest_v1():
    payload = NoResult()

    with (
        patch("app.actions.slack_post.get_token", AsyncMock(return_value="xoxb-fake-token")),
        patch("app.actions.slack_post.read_upstream", AsyncMock(return_value=payload)),
        _patch_http_slack(),
    ):
        handler = SlackPostHandler()
        params = SlackPostParams(channel="#c", from_run_id=42, template=SlackTemplate.digest_v1)
        result = await handler.execute(run=FakeRun(), params=params)

    assert result.ok is True


@pytest.mark.asyncio
async def test_from_run_id_invalid_json_interview_brief():
    payload = InvalidJson(raw='{"bad": }')

    with (
        patch("app.actions.slack_post.get_token", AsyncMock(return_value="xoxb-fake-token")),
        patch("app.actions.slack_post.read_upstream", AsyncMock(return_value=payload)),
        _patch_http_slack(),
    ):
        handler = SlackPostHandler()
        params = SlackPostParams(
            channel="#c", from_run_id=42, template=SlackTemplate.interview_brief
        )
        result = await handler.execute(run=FakeRun(), params=params)

    assert result.ok is True


# ---------------------------------------------------------------------------
# Template tests — each template on ok-path and error-path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_digest_v1_ok_path_formats_data():
    data = {"PRs": 3, "Issues": 7}
    ctx, posted_payloads = _capture_http_post()

    with (
        patch("app.actions.slack_post.get_token", AsyncMock(return_value="xoxb-fake-token")),
        patch("app.actions.slack_post.read_upstream", AsyncMock(return_value=Ok(data=data))),
        patch("app.actions.slack_post.httpx.AsyncClient", return_value=ctx),
    ):
        handler = SlackPostHandler()
        params = SlackPostParams(channel="#c", from_run_id=1, template=SlackTemplate.digest_v1)
        result = await handler.execute(run=FakeRun(), params=params)

    assert result.ok is True
    assert posted_payloads
    text = posted_payloads[0]["text"]
    assert "*Daily Digest*" in text
    assert "PRs" in text


@pytest.mark.asyncio
async def test_digest_v1_error_path_shows_warning():
    payload = UpstreamError(error_msg="github rate limited")
    ctx, posted_payloads = _capture_http_post()

    with (
        patch("app.actions.slack_post.get_token", AsyncMock(return_value="xoxb-fake-token")),
        patch("app.actions.slack_post.read_upstream", AsyncMock(return_value=payload)),
        patch("app.actions.slack_post.httpx.AsyncClient", return_value=ctx),
    ):
        handler = SlackPostHandler()
        params = SlackPostParams(channel="#c", from_run_id=1, template=SlackTemplate.digest_v1)
        result = await handler.execute(run=FakeRun(), params=params)

    assert result.ok is True
    text = posted_payloads[0]["text"]
    assert "⚠" in text
    assert "Digest unavailable" in text


@pytest.mark.asyncio
async def test_interview_brief_ok_path_formats_data():
    data = {"Candidate": "Alice", "Role": "SWE"}
    ctx, posted_payloads = _capture_http_post()

    with (
        patch("app.actions.slack_post.get_token", AsyncMock(return_value="xoxb-fake-token")),
        patch("app.actions.slack_post.read_upstream", AsyncMock(return_value=Ok(data=data))),
        patch("app.actions.slack_post.httpx.AsyncClient", return_value=ctx),
    ):
        handler = SlackPostHandler()
        params = SlackPostParams(
            channel="#c", from_run_id=1, template=SlackTemplate.interview_brief
        )
        result = await handler.execute(run=FakeRun(), params=params)

    assert result.ok is True
    text = posted_payloads[0]["text"]
    assert "*Interview Brief*" in text
    assert "Candidate" in text


@pytest.mark.asyncio
async def test_interview_brief_error_path_shows_warning():
    payload = UpstreamError(error_msg="calendar unavailable")
    ctx, posted_payloads = _capture_http_post()

    with (
        patch("app.actions.slack_post.get_token", AsyncMock(return_value="xoxb-fake-token")),
        patch("app.actions.slack_post.read_upstream", AsyncMock(return_value=payload)),
        patch("app.actions.slack_post.httpx.AsyncClient", return_value=ctx),
    ):
        handler = SlackPostHandler()
        params = SlackPostParams(
            channel="#c", from_run_id=1, template=SlackTemplate.interview_brief
        )
        result = await handler.execute(run=FakeRun(), params=params)

    assert result.ok is True
    text = posted_payloads[0]["text"]
    assert "⚠" in text
    assert "Interview brief unavailable" in text


@pytest.mark.asyncio
async def test_raw_ok_path():
    data = "raw upstream text"
    ctx, posted_payloads = _capture_http_post()

    with (
        patch("app.actions.slack_post.get_token", AsyncMock(return_value="xoxb-fake-token")),
        patch("app.actions.slack_post.read_upstream", AsyncMock(return_value=Ok(data=data))),
        patch("app.actions.slack_post.httpx.AsyncClient", return_value=ctx),
    ):
        handler = SlackPostHandler()
        params = SlackPostParams(channel="#c", from_run_id=1, template=SlackTemplate.raw)
        result = await handler.execute(run=FakeRun(), params=params)

    assert result.ok is True
    text = posted_payloads[0]["text"]
    assert "raw upstream text" in text


@pytest.mark.asyncio
async def test_raw_error_path():
    payload = UpstreamError(error_msg="timeout error")
    ctx, posted_payloads = _capture_http_post()

    with (
        patch("app.actions.slack_post.get_token", AsyncMock(return_value="xoxb-fake-token")),
        patch("app.actions.slack_post.read_upstream", AsyncMock(return_value=payload)),
        patch("app.actions.slack_post.httpx.AsyncClient", return_value=ctx),
    ):
        handler = SlackPostHandler()
        params = SlackPostParams(channel="#c", from_run_id=1, template=SlackTemplate.raw)
        result = await handler.execute(run=FakeRun(), params=params)

    assert result.ok is True
    text = posted_payloads[0]["text"]
    assert "⚠" in text
    assert "timeout error" in text


# ---------------------------------------------------------------------------
# Neither message nor from_run_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slack_post_no_message_no_from_run_id_fails():
    handler = SlackPostHandler()
    params = SlackPostParams(channel="#c")
    run = FakeRun()

    with patch("app.actions.slack_post.get_token", AsyncMock(return_value="xoxb-fake-token")):
        result = await handler.execute(run=run, params=params)

    assert result.ok is False
    assert result.retryable is False
    assert "message" in (result.error or "").lower() or "from_run_id" in (result.error or "")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_slack_post_registered_in_registry():
    assert "slack_post" in ACTION_REGISTRY
    assert isinstance(ACTION_REGISTRY["slack_post"], SlackPostHandler)


def test_list_actions_returns_three_actions():
    """task.list_actions.v1 now exposes echo, http_call, and slack_post."""
    assert len(ACTION_REGISTRY) >= 3
    assert "echo" in ACTION_REGISTRY
    assert "http_call" in ACTION_REGISTRY
    assert "slack_post" in ACTION_REGISTRY
