"""Unit tests for the slack_post action handler (no real network or DB)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

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

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_http_client(status_code: int = 200, body: str = "ok") -> MagicMock:
    """Return a mock AsyncClient context that replies with the given status."""
    response = httpx.Response(status_code, content=body.encode())
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=response)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_client)
    ctx.__aexit__ = AsyncMock(return_value=None)
    return ctx


def _patch_http(status_code: int = 200, body: str = "ok"):
    ctx = _mock_http_client(status_code, body)
    return patch("app.actions.slack_post.httpx.AsyncClient", return_value=ctx)


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
# SlackPostHandler — happy POST
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slack_post_200_ok():
    handler = SlackPostHandler()
    params = SlackPostParams(channel="#general", message="hello")

    with patch.dict("os.environ", {"SLACK_WEBHOOK_URL": "https://hooks.slack.com/test"}):
        with _patch_http(200, "ok"):
            result = await handler.execute(run=None, params=params)

    assert result.ok is True
    assert result.retryable is False
    assert result.error is None
    assert result.result is not None
    assert result.result["status_code"] == 200


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slack_post_429_is_retryable():
    handler = SlackPostHandler()
    params = SlackPostParams(channel="#c", message="m")

    with patch.dict("os.environ", {"SLACK_WEBHOOK_URL": "https://hooks.slack.com/test"}):
        with _patch_http(429, "rate limited"):
            result = await handler.execute(run=None, params=params)

    assert result.ok is False
    assert result.retryable is True
    assert "429" in (result.error or "")


@pytest.mark.asyncio
async def test_slack_post_401_not_retryable():
    handler = SlackPostHandler()
    params = SlackPostParams(channel="#c", message="m")

    with patch.dict("os.environ", {"SLACK_WEBHOOK_URL": "https://hooks.slack.com/test"}):
        with _patch_http(401, "unauthorized"):
            result = await handler.execute(run=None, params=params)

    assert result.ok is False
    assert result.retryable is False


@pytest.mark.asyncio
async def test_slack_post_403_not_retryable():
    handler = SlackPostHandler()
    params = SlackPostParams(channel="#c", message="m")

    with patch.dict("os.environ", {"SLACK_WEBHOOK_URL": "https://hooks.slack.com/test"}):
        with _patch_http(403, "forbidden"):
            result = await handler.execute(run=None, params=params)

    assert result.ok is False
    assert result.retryable is False


@pytest.mark.asyncio
async def test_slack_post_404_not_retryable():
    handler = SlackPostHandler()
    params = SlackPostParams(channel="#c", message="m")

    with patch.dict("os.environ", {"SLACK_WEBHOOK_URL": "https://hooks.slack.com/test"}):
        with _patch_http(404, "not found"):
            result = await handler.execute(run=None, params=params)

    assert result.ok is False
    assert result.retryable is False


@pytest.mark.asyncio
async def test_slack_post_410_not_retryable():
    handler = SlackPostHandler()
    params = SlackPostParams(channel="#c", message="m")

    with patch.dict("os.environ", {"SLACK_WEBHOOK_URL": "https://hooks.slack.com/test"}):
        with _patch_http(410, "gone"):
            result = await handler.execute(run=None, params=params)

    assert result.ok is False
    assert result.retryable is False


@pytest.mark.asyncio
async def test_slack_post_500_is_retryable():
    handler = SlackPostHandler()
    params = SlackPostParams(channel="#c", message="m")

    with patch.dict("os.environ", {"SLACK_WEBHOOK_URL": "https://hooks.slack.com/test"}):
        with _patch_http(500, "server error"):
            result = await handler.execute(run=None, params=params)

    assert result.ok is False
    assert result.retryable is True


@pytest.mark.asyncio
async def test_slack_post_timeout_is_retryable():
    handler = SlackPostHandler()
    params = SlackPostParams(channel="#c", message="m")

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_client)
    ctx.__aexit__ = AsyncMock(return_value=None)

    with patch.dict("os.environ", {"SLACK_WEBHOOK_URL": "https://hooks.slack.com/test"}):
        with patch("app.actions.slack_post.httpx.AsyncClient", return_value=ctx):
            result = await handler.execute(run=None, params=params)

    assert result.ok is False
    assert result.retryable is True
    assert "Timeout" in (result.error or "")


@pytest.mark.asyncio
async def test_slack_post_network_error_is_retryable():
    handler = SlackPostHandler()
    params = SlackPostParams(channel="#c", message="m")

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_client)
    ctx.__aexit__ = AsyncMock(return_value=None)

    with patch.dict("os.environ", {"SLACK_WEBHOOK_URL": "https://hooks.slack.com/test"}):
        with patch("app.actions.slack_post.httpx.AsyncClient", return_value=ctx):
            result = await handler.execute(run=None, params=params)

    assert result.ok is False
    assert result.retryable is True


# ---------------------------------------------------------------------------
# SLACK_WEBHOOK_URL missing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slack_post_missing_webhook_url_fails():
    handler = SlackPostHandler()
    params = SlackPostParams(channel="#c", message="m")

    env = {k: v for k, v in __import__("os").environ.items() if k != "SLACK_WEBHOOK_URL"}
    with patch.dict("os.environ", env, clear=True):
        result = await handler.execute(run=None, params=params)

    assert result.ok is False
    assert result.retryable is False
    assert "SLACK_WEBHOOK_URL" in (result.error or "")


# ---------------------------------------------------------------------------
# Secrets resolution in execute()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slack_post_resolves_webhook_url_via_env():
    """SLACK_WEBHOOK_URL in env is used as the POST target."""
    handler = SlackPostHandler()
    params = SlackPostParams(channel="#c", message="hello")

    captured_url: list[str] = []

    async def fake_post(url: str, **kwargs: object) -> httpx.Response:
        captured_url.append(url)
        return httpx.Response(200, content=b"ok")

    mock_client = AsyncMock()
    mock_client.post = fake_post
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_client)
    ctx.__aexit__ = AsyncMock(return_value=None)

    webhook = "https://hooks.slack.com/services/T000/B000/xxx"
    with patch.dict("os.environ", {"SLACK_WEBHOOK_URL": webhook}):
        with patch("app.actions.slack_post.httpx.AsyncClient", return_value=ctx):
            result = await handler.execute(run=None, params=params)

    assert result.ok is True
    assert captured_url == [webhook]


# ---------------------------------------------------------------------------
# from_run_id — upstream variants via mocked read_upstream
# ---------------------------------------------------------------------------


def _make_mock_session_factory(upstream_payload: object) -> Any:
    """Build a session factory that makes read_upstream return *upstream_payload*."""
    mock_session = AsyncMock()
    mock_session.begin = MagicMock(return_value=mock_session)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    mock_factory_ctx = AsyncMock()
    mock_factory_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_factory_ctx.__aexit__ = AsyncMock(return_value=None)

    mock_factory = MagicMock(return_value=mock_factory_ctx)
    return mock_factory, mock_session


@pytest.mark.asyncio
async def test_from_run_id_ok_raw_template():
    data = {"summary": "5 issues closed"}
    mock_factory, mock_session = _make_mock_session_factory(Ok(data=data))

    with patch("app.actions.slack_post.read_upstream", AsyncMock(return_value=Ok(data=data))):
        with patch.dict("os.environ", {"SLACK_WEBHOOK_URL": "https://hooks.slack.com/test"}):
            with _patch_http(200):
                handler = SlackPostHandler(session_factory=mock_factory)
                params = SlackPostParams(channel="#c", from_run_id=42, template=SlackTemplate.raw)
                result = await handler.execute(run=None, params=params)

    assert result.ok is True


@pytest.mark.asyncio
async def test_from_run_id_upstream_error_raw_template():
    payload = UpstreamError(error_msg="upstream failed")
    mock_factory, _ = _make_mock_session_factory(payload)

    with patch("app.actions.slack_post.read_upstream", AsyncMock(return_value=payload)):
        with patch.dict("os.environ", {"SLACK_WEBHOOK_URL": "https://hooks.slack.com/test"}):
            with _patch_http(200):
                handler = SlackPostHandler(session_factory=mock_factory)
                params = SlackPostParams(channel="#c", from_run_id=42, template=SlackTemplate.raw)
                result = await handler.execute(run=None, params=params)

    assert result.ok is True  # POST succeeded even with error-path formatting


@pytest.mark.asyncio
async def test_from_run_id_no_result_digest_v1():
    payload = NoResult()
    mock_factory, _ = _make_mock_session_factory(payload)

    with patch("app.actions.slack_post.read_upstream", AsyncMock(return_value=payload)):
        with patch.dict("os.environ", {"SLACK_WEBHOOK_URL": "https://hooks.slack.com/test"}):
            with _patch_http(200):
                handler = SlackPostHandler(session_factory=mock_factory)
                params = SlackPostParams(
                    channel="#c", from_run_id=42, template=SlackTemplate.digest_v1
                )
                result = await handler.execute(run=None, params=params)

    assert result.ok is True


@pytest.mark.asyncio
async def test_from_run_id_invalid_json_interview_brief():
    payload = InvalidJson(raw='{"bad": }')
    mock_factory, _ = _make_mock_session_factory(payload)

    with patch("app.actions.slack_post.read_upstream", AsyncMock(return_value=payload)):
        with patch.dict("os.environ", {"SLACK_WEBHOOK_URL": "https://hooks.slack.com/test"}):
            with _patch_http(200):
                handler = SlackPostHandler(session_factory=mock_factory)
                params = SlackPostParams(
                    channel="#c", from_run_id=42, template=SlackTemplate.interview_brief
                )
                result = await handler.execute(run=None, params=params)

    assert result.ok is True


# ---------------------------------------------------------------------------
# Template tests — each template on ok-path and error-path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_digest_v1_ok_path_formats_data():
    data = {"PRs": 3, "Issues": 7}
    mock_factory, _ = _make_mock_session_factory(Ok(data=data))
    posted_payloads: list[dict] = []

    async def capture_post(url: str, **kwargs: object) -> httpx.Response:
        posted_payloads.append(kwargs.get("json", {}))
        return httpx.Response(200, content=b"ok")

    mock_client = AsyncMock()
    mock_client.post = capture_post
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_client)
    ctx.__aexit__ = AsyncMock(return_value=None)

    with patch("app.actions.slack_post.read_upstream", AsyncMock(return_value=Ok(data=data))):
        with patch.dict("os.environ", {"SLACK_WEBHOOK_URL": "https://hooks.slack.com/test"}):
            with patch("app.actions.slack_post.httpx.AsyncClient", return_value=ctx):
                handler = SlackPostHandler(session_factory=mock_factory)
                params = SlackPostParams(
                    channel="#c", from_run_id=1, template=SlackTemplate.digest_v1
                )
                result = await handler.execute(run=None, params=params)

    assert result.ok is True
    assert posted_payloads
    text = posted_payloads[0]["text"]
    assert "*Daily Digest*" in text
    assert "PRs" in text


@pytest.mark.asyncio
async def test_digest_v1_error_path_shows_warning():
    payload = UpstreamError(error_msg="github rate limited")
    mock_factory, _ = _make_mock_session_factory(payload)
    posted_payloads: list[dict] = []

    async def capture_post(url: str, **kwargs: object) -> httpx.Response:
        posted_payloads.append(kwargs.get("json", {}))
        return httpx.Response(200, content=b"ok")

    mock_client = AsyncMock()
    mock_client.post = capture_post
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_client)
    ctx.__aexit__ = AsyncMock(return_value=None)

    with patch("app.actions.slack_post.read_upstream", AsyncMock(return_value=payload)):
        with patch.dict("os.environ", {"SLACK_WEBHOOK_URL": "https://hooks.slack.com/test"}):
            with patch("app.actions.slack_post.httpx.AsyncClient", return_value=ctx):
                handler = SlackPostHandler(session_factory=mock_factory)
                params = SlackPostParams(
                    channel="#c", from_run_id=1, template=SlackTemplate.digest_v1
                )
                result = await handler.execute(run=None, params=params)

    assert result.ok is True
    text = posted_payloads[0]["text"]
    assert "⚠" in text
    assert "Digest unavailable" in text


@pytest.mark.asyncio
async def test_interview_brief_ok_path_formats_data():
    data = {"Candidate": "Alice", "Role": "SWE"}
    mock_factory, _ = _make_mock_session_factory(Ok(data=data))
    posted_payloads: list[dict] = []

    async def capture_post(url: str, **kwargs: object) -> httpx.Response:
        posted_payloads.append(kwargs.get("json", {}))
        return httpx.Response(200, content=b"ok")

    mock_client = AsyncMock()
    mock_client.post = capture_post
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_client)
    ctx.__aexit__ = AsyncMock(return_value=None)

    with patch("app.actions.slack_post.read_upstream", AsyncMock(return_value=Ok(data=data))):
        with patch.dict("os.environ", {"SLACK_WEBHOOK_URL": "https://hooks.slack.com/test"}):
            with patch("app.actions.slack_post.httpx.AsyncClient", return_value=ctx):
                handler = SlackPostHandler(session_factory=mock_factory)
                params = SlackPostParams(
                    channel="#c", from_run_id=1, template=SlackTemplate.interview_brief
                )
                result = await handler.execute(run=None, params=params)

    assert result.ok is True
    text = posted_payloads[0]["text"]
    assert "*Interview Brief*" in text
    assert "Candidate" in text


@pytest.mark.asyncio
async def test_interview_brief_error_path_shows_warning():
    payload = UpstreamError(error_msg="calendar unavailable")
    mock_factory, _ = _make_mock_session_factory(payload)
    posted_payloads: list[dict] = []

    async def capture_post(url: str, **kwargs: object) -> httpx.Response:
        posted_payloads.append(kwargs.get("json", {}))
        return httpx.Response(200, content=b"ok")

    mock_client = AsyncMock()
    mock_client.post = capture_post
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_client)
    ctx.__aexit__ = AsyncMock(return_value=None)

    with patch("app.actions.slack_post.read_upstream", AsyncMock(return_value=payload)):
        with patch.dict("os.environ", {"SLACK_WEBHOOK_URL": "https://hooks.slack.com/test"}):
            with patch("app.actions.slack_post.httpx.AsyncClient", return_value=ctx):
                handler = SlackPostHandler(session_factory=mock_factory)
                params = SlackPostParams(
                    channel="#c", from_run_id=1, template=SlackTemplate.interview_brief
                )
                result = await handler.execute(run=None, params=params)

    assert result.ok is True
    text = posted_payloads[0]["text"]
    assert "⚠" in text
    assert "Interview brief unavailable" in text


@pytest.mark.asyncio
async def test_raw_ok_path():
    data = "raw upstream text"
    mock_factory, _ = _make_mock_session_factory(Ok(data=data))
    posted_payloads: list[dict] = []

    async def capture_post(url: str, **kwargs: object) -> httpx.Response:
        posted_payloads.append(kwargs.get("json", {}))
        return httpx.Response(200, content=b"ok")

    mock_client = AsyncMock()
    mock_client.post = capture_post
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_client)
    ctx.__aexit__ = AsyncMock(return_value=None)

    with patch("app.actions.slack_post.read_upstream", AsyncMock(return_value=Ok(data=data))):
        with patch.dict("os.environ", {"SLACK_WEBHOOK_URL": "https://hooks.slack.com/test"}):
            with patch("app.actions.slack_post.httpx.AsyncClient", return_value=ctx):
                handler = SlackPostHandler(session_factory=mock_factory)
                params = SlackPostParams(channel="#c", from_run_id=1, template=SlackTemplate.raw)
                result = await handler.execute(run=None, params=params)

    assert result.ok is True
    text = posted_payloads[0]["text"]
    assert "raw upstream text" in text


@pytest.mark.asyncio
async def test_raw_error_path():
    payload = UpstreamError(error_msg="timeout error")
    mock_factory, _ = _make_mock_session_factory(payload)
    posted_payloads: list[dict] = []

    async def capture_post(url: str, **kwargs: object) -> httpx.Response:
        posted_payloads.append(kwargs.get("json", {}))
        return httpx.Response(200, content=b"ok")

    mock_client = AsyncMock()
    mock_client.post = capture_post
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_client)
    ctx.__aexit__ = AsyncMock(return_value=None)

    with patch("app.actions.slack_post.read_upstream", AsyncMock(return_value=payload)):
        with patch.dict("os.environ", {"SLACK_WEBHOOK_URL": "https://hooks.slack.com/test"}):
            with patch("app.actions.slack_post.httpx.AsyncClient", return_value=ctx):
                handler = SlackPostHandler(session_factory=mock_factory)
                params = SlackPostParams(channel="#c", from_run_id=1, template=SlackTemplate.raw)
                result = await handler.execute(run=None, params=params)

    assert result.ok is True
    text = posted_payloads[0]["text"]
    assert "⚠" in text
    assert "timeout error" in text


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
