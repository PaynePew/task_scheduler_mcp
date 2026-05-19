"""Unit tests for app/chain/upstream_reader.py.

Covers all four UpstreamPayload variants using a mocked AsyncSession.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.chain.upstream_reader import (
    InvalidJson,
    NoResult,
    Ok,
    UpstreamError,
    read_upstream,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_session(run: object | None) -> AsyncMock:
    scalars = MagicMock()
    scalars.first.return_value = run
    execute_result = MagicMock()
    execute_result.scalars.return_value = scalars
    session = AsyncMock()
    session.execute = AsyncMock(return_value=execute_result)
    return session


def _make_run(result: str | None = None, error_message: str | None = None) -> MagicMock:
    run = MagicMock()
    run.run_id = 1
    run.result = result
    run.error_message = error_message
    return run


# ---------------------------------------------------------------------------
# NoResult — run not found
# ---------------------------------------------------------------------------


async def test_no_result_when_run_not_found():
    session = _make_session(None)
    payload = await read_upstream(999, session)
    assert isinstance(payload, NoResult)


# ---------------------------------------------------------------------------
# NoResult — run found with result=NULL and no error_message
# ---------------------------------------------------------------------------


async def test_no_result_when_result_null_no_error():
    run = _make_run(result=None, error_message=None)
    session = _make_session(run)
    payload = await read_upstream(1, session)
    assert isinstance(payload, NoResult)


# ---------------------------------------------------------------------------
# UpstreamError — result=NULL but error_message is set
# ---------------------------------------------------------------------------


async def test_upstream_error_when_result_null_with_error_message():
    run = _make_run(result=None, error_message="connection refused")
    session = _make_session(run)
    payload = await read_upstream(1, session)
    assert isinstance(payload, UpstreamError)
    assert payload.error_msg == "connection refused"


# ---------------------------------------------------------------------------
# Ok — result is valid JSON
# ---------------------------------------------------------------------------


async def test_ok_when_result_is_valid_json_object():
    data = {"status": "ok", "count": 42}
    run = _make_run(result=json.dumps(data))
    session = _make_session(run)
    payload = await read_upstream(1, session)
    assert isinstance(payload, Ok)
    assert payload.data == data


async def test_ok_when_result_is_valid_json_array():
    data = [1, 2, 3]
    run = _make_run(result=json.dumps(data))
    session = _make_session(run)
    payload = await read_upstream(1, session)
    assert isinstance(payload, Ok)
    assert payload.data == data


async def test_ok_when_result_is_valid_json_string():
    run = _make_run(result=json.dumps("hello"))
    session = _make_session(run)
    payload = await read_upstream(1, session)
    assert isinstance(payload, Ok)
    assert payload.data == "hello"


async def test_ok_when_result_is_valid_json_number():
    run = _make_run(result="123")
    session = _make_session(run)
    payload = await read_upstream(1, session)
    assert isinstance(payload, Ok)
    assert payload.data == 123


# ---------------------------------------------------------------------------
# InvalidJson — result is non-NULL but not valid JSON
# ---------------------------------------------------------------------------


async def test_invalid_json_when_result_is_not_json():
    run = _make_run(result="not-json{{")
    session = _make_session(run)
    payload = await read_upstream(1, session)
    assert isinstance(payload, InvalidJson)
    assert payload.raw == "not-json{{"


async def test_invalid_json_when_result_is_truncated():
    run = _make_run(result='{"key": ')
    session = _make_session(run)
    payload = await read_upstream(1, session)
    assert isinstance(payload, InvalidJson)
    assert payload.raw == '{"key": '


# ---------------------------------------------------------------------------
# Type union — payload is one of the four variants
# ---------------------------------------------------------------------------


async def test_payload_variants_are_distinct_types():
    """Each variant is a distinct class; isinstance checks work as expected."""
    assert not isinstance(NoResult(), Ok)
    assert not isinstance(NoResult(), UpstreamError)
    assert not isinstance(NoResult(), InvalidJson)
    assert not isinstance(Ok(data={}), NoResult)
    assert not isinstance(UpstreamError(error_msg="e"), Ok)
    assert not isinstance(InvalidJson(raw="x"), Ok)


async def test_ok_is_frozen():
    """Ok is a frozen dataclass — mutation raises AttributeError."""
    payload = Ok(data={"k": "v"})
    with pytest.raises(AttributeError):
        payload.data = "new"  # type: ignore[misc]
