"""Unit tests for resolve_for_display and resolve_or_terminal helpers.

Covers all 4 UpstreamPayload variants × each helper, plus both on_invalid_json
modes for resolve_or_terminal — mocking read_upstream to isolate dispatch logic.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, patch

from app.actions.base import ActionResult
from app.chain.upstream_reader import (
    InvalidJson,
    NoResult,
    Ok,
    UpstreamError,
    resolve_for_display,
    resolve_or_terminal,
)

# ---------------------------------------------------------------------------
# Shared formatter for display tests
# ---------------------------------------------------------------------------


def _fmt(data: Any, *, is_error: bool) -> str:
    return f"data={data!r},is_error={is_error}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _patch_upstream(payload):
    """Context manager: patch read_upstream to return *payload*."""
    return patch(
        "app.chain.upstream_reader.read_upstream",
        new_callable=AsyncMock,
        return_value=payload,
    )


# ---------------------------------------------------------------------------
# resolve_for_display — 4 variants
# ---------------------------------------------------------------------------


async def test_resolve_for_display_ok():
    with _patch_upstream(Ok(data={"x": 1})):
        result = await resolve_for_display(1, AsyncMock(), formatter=_fmt)
    assert result == "data={'x': 1},is_error=False"


async def test_resolve_for_display_upstream_error():
    with _patch_upstream(UpstreamError(error_msg="boom")):
        result = await resolve_for_display(1, AsyncMock(), formatter=_fmt)
    assert result == "data='boom',is_error=True"


async def test_resolve_for_display_no_result():
    with _patch_upstream(NoResult()):
        result = await resolve_for_display(1, AsyncMock(), formatter=_fmt)
    assert result == "data='(no result)',is_error=True"


async def test_resolve_for_display_invalid_json():
    raw = "bad-json"
    with _patch_upstream(InvalidJson(raw=raw)):
        result = await resolve_for_display(1, AsyncMock(), formatter=_fmt)
    assert result == f"data='(invalid JSON: {raw})',is_error=True"


async def test_resolve_for_display_invalid_json_truncates_at_100():
    raw = "x" * 200
    with _patch_upstream(InvalidJson(raw=raw)):
        result = await resolve_for_display(1, AsyncMock(), formatter=_fmt)
    truncated = raw[:100]
    assert result == f"data='(invalid JSON: {truncated})',is_error=True"


# ---------------------------------------------------------------------------
# resolve_or_terminal — 4 variants × 2 on_invalid_json modes
# ---------------------------------------------------------------------------


async def test_resolve_or_terminal_ok_dict_accept_raw():
    data = {"key": "val"}
    with _patch_upstream(Ok(data=data)):
        result = await resolve_or_terminal(1, AsyncMock(), on_invalid_json="accept_raw")
    assert result == json.dumps(data)


async def test_resolve_or_terminal_ok_dict_fail():
    data = {"key": "val"}
    with _patch_upstream(Ok(data=data)):
        result = await resolve_or_terminal(1, AsyncMock(), on_invalid_json="fail")
    assert result == json.dumps(data)


async def test_resolve_or_terminal_ok_str_accept_raw():
    with _patch_upstream(Ok(data="plain text")):
        result = await resolve_or_terminal(1, AsyncMock(), on_invalid_json="accept_raw")
    assert result == "plain text"


async def test_resolve_or_terminal_ok_str_fail():
    with _patch_upstream(Ok(data="plain text")):
        result = await resolve_or_terminal(1, AsyncMock(), on_invalid_json="fail")
    assert result == "plain text"


async def test_resolve_or_terminal_ok_list_accept_raw():
    data = [1, 2, 3]
    with _patch_upstream(Ok(data=data)):
        result = await resolve_or_terminal(1, AsyncMock(), on_invalid_json="accept_raw")
    assert result == json.dumps(data)


async def test_resolve_or_terminal_ok_list_fail():
    data = [1, 2, 3]
    with _patch_upstream(Ok(data=data)):
        result = await resolve_or_terminal(1, AsyncMock(), on_invalid_json="fail")
    assert result == json.dumps(data)


async def test_resolve_or_terminal_upstream_error_accept_raw():
    with _patch_upstream(UpstreamError(error_msg="db down")):
        result = await resolve_or_terminal(1, AsyncMock(), on_invalid_json="accept_raw")
    assert isinstance(result, ActionResult)
    assert result.ok is False
    assert result.retryable is False
    assert "db down" in result.error


async def test_resolve_or_terminal_upstream_error_fail():
    with _patch_upstream(UpstreamError(error_msg="db down")):
        result = await resolve_or_terminal(1, AsyncMock(), on_invalid_json="fail")
    assert isinstance(result, ActionResult)
    assert result.ok is False
    assert result.retryable is False
    assert "db down" in result.error


async def test_resolve_or_terminal_no_result_accept_raw():
    with _patch_upstream(NoResult()):
        result = await resolve_or_terminal(1, AsyncMock(), on_invalid_json="accept_raw")
    assert isinstance(result, ActionResult)
    assert result.ok is False
    assert result.retryable is False


async def test_resolve_or_terminal_no_result_fail():
    with _patch_upstream(NoResult()):
        result = await resolve_or_terminal(1, AsyncMock(), on_invalid_json="fail")
    assert isinstance(result, ActionResult)
    assert result.ok is False
    assert result.retryable is False


async def test_resolve_or_terminal_invalid_json_accept_raw():
    raw = '{"broken": '
    with _patch_upstream(InvalidJson(raw=raw)):
        result = await resolve_or_terminal(1, AsyncMock(), on_invalid_json="accept_raw")
    assert result == raw


async def test_resolve_or_terminal_invalid_json_fail():
    raw = '{"broken": '
    with _patch_upstream(InvalidJson(raw=raw)):
        result = await resolve_or_terminal(1, AsyncMock(), on_invalid_json="fail")
    assert isinstance(result, ActionResult)
    assert result.ok is False
    assert result.retryable is False
