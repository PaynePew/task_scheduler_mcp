"""Unit tests for app/mcp/resources/recent_results.py."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.mcp.resources.recent_results import (
    _TERMINAL_STATUSES,
    _MAX_ROWS,
    read_tasks_recent_results,
)


def _make_row(
    *,
    run_id: int = 1,
    job_id: int = 10,
    status: str = "SUCCEEDED",
    action: str = "echo",
    start_at: datetime | None = None,
    finish_at: datetime | None = None,
    error_message: str | None = None,
) -> Any:
    """Build a mock row object matching the columns returned by _query_recent_runs."""
    now = datetime.now(tz=UTC)
    row = MagicMock()
    row.run_id = run_id
    row.job_id = job_id
    row.status = status
    row.action = action
    row.start_at = start_at or now - timedelta(minutes=5)
    row.finish_at = finish_at or now
    row.error_message = error_message
    return row


# ---------------------------------------------------------------------------
# Helpers: patch target
# ---------------------------------------------------------------------------

_PATCH = "app.mcp.resources.recent_results._query_recent_runs"


# ---------------------------------------------------------------------------
# Empty-result case
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch(_PATCH, new_callable=AsyncMock)
async def test_empty_result_returns_empty_runs(mock_query):
    """Empty DB result produces a valid payload with an empty runs list."""
    mock_query.return_value = []

    contents = await read_tasks_recent_results("user-1")

    assert len(contents) == 1
    payload = json.loads(contents[0].content)
    assert payload["user_id"] == "user-1"
    assert payload["window_hours"] == 24
    assert payload["runs"] == []
    assert "queried_at" in payload
    assert contents[0].mime_type == "application/json"


# ---------------------------------------------------------------------------
# Single-run case
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch(_PATCH, new_callable=AsyncMock)
async def test_single_run_correct_shape(mock_query):
    """A single terminal run is serialised with the required fields."""
    finish = datetime(2026, 5, 19, 10, 0, 0, tzinfo=UTC)
    start = datetime(2026, 5, 19, 9, 55, 0, tzinfo=UTC)
    row = _make_row(run_id=42, job_id=7, status="SUCCEEDED", action="echo",
                    start_at=start, finish_at=finish)
    mock_query.return_value = [row]

    contents = await read_tasks_recent_results("user-1")
    payload = json.loads(contents[0].content)

    assert len(payload["runs"]) == 1
    run = payload["runs"][0]
    assert run["run_id"] == 42
    assert run["job_id"] == 7
    assert run["action"] == "echo"
    assert run["status"] == "completed"
    assert run["finish_at"] == finish.isoformat()
    assert run["start_at"] == start.isoformat()
    assert run["error_excerpt"] is None


# ---------------------------------------------------------------------------
# Cap at 50 rows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch(_PATCH, new_callable=AsyncMock)
async def test_cap_at_max_rows(mock_query):
    """The resource returns at most _MAX_ROWS runs (50-row cap)."""
    # The SQL LIMIT is enforced by the query; _query_recent_runs returns at
    # most 50 rows.  We mock it to return exactly 50 to verify the payload cap.
    rows = [_make_row(run_id=i, job_id=i) for i in range(_MAX_ROWS)]
    mock_query.return_value = rows

    contents = await read_tasks_recent_results("user-1")
    payload = json.loads(contents[0].content)

    assert len(payload["runs"]) == _MAX_ROWS


@pytest.mark.asyncio
@patch(_PATCH, new_callable=AsyncMock)
async def test_resource_does_not_exceed_cap(mock_query):
    """If the query (incorrectly) returns more than 50, the resource still caps."""
    # Guard against future bugs where the LIMIT is accidentally removed.
    rows = [_make_row(run_id=i, job_id=i) for i in range(_MAX_ROWS + 10)]
    mock_query.return_value = rows

    contents = await read_tasks_recent_results("user-1")
    payload = json.loads(contents[0].content)

    assert len(payload["runs"]) == _MAX_ROWS


# ---------------------------------------------------------------------------
# Status filtering: all three terminal statuses map correctly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch(_PATCH, new_callable=AsyncMock)
async def test_terminal_statuses_map_to_external(mock_query):
    """SUCCEEDED, FAILED, and CANCELLED map to completed, failed, cancelled."""
    rows = [
        _make_row(run_id=1, status="SUCCEEDED"),
        _make_row(run_id=2, status="FAILED"),
        _make_row(run_id=3, status="CANCELLED"),
    ]
    mock_query.return_value = rows

    contents = await read_tasks_recent_results("user-1")
    payload = json.loads(contents[0].content)

    statuses = {r["run_id"]: r["status"] for r in payload["runs"]}
    assert statuses[1] == "completed"
    assert statuses[2] == "failed"
    assert statuses[3] == "cancelled"


def test_terminal_statuses_constant():
    """The module-level constant covers exactly SUCCEEDED, FAILED, CANCELLED."""
    assert _TERMINAL_STATUSES == frozenset({"SUCCEEDED", "FAILED", "CANCELLED"})


# ---------------------------------------------------------------------------
# user_id scoping: confirm user_id is forwarded to the query helper
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch(_PATCH, new_callable=AsyncMock)
async def test_user_id_forwarded_to_query(mock_query):
    """read_tasks_recent_results passes the caller's user_id to _query_recent_runs."""
    mock_query.return_value = []

    await read_tasks_recent_results("alice")

    called_kwargs = mock_query.call_args
    # _query_recent_runs(session, user_id=..., cutoff=...)
    assert called_kwargs.kwargs["user_id"] == "alice"


@pytest.mark.asyncio
@patch(_PATCH, new_callable=AsyncMock)
async def test_different_user_ids_passed_through(mock_query):
    """user_id is not hardcoded — different callers pass their own value."""
    mock_query.return_value = []

    await read_tasks_recent_results("bob")
    assert mock_query.call_args.kwargs["user_id"] == "bob"

    await read_tasks_recent_results("carol")
    assert mock_query.call_args.kwargs["user_id"] == "carol"


# ---------------------------------------------------------------------------
# Ordering: output preserves DB row order (SQL enforces finish_at DESC)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch(_PATCH, new_callable=AsyncMock)
async def test_output_preserves_row_order(mock_query):
    """The resource preserves the order returned by _query_recent_runs.

    The SQL ORDER BY finish_at DESC is tested in integration; here we verify
    the resource layer doesn't re-sort or shuffle the rows it receives.
    """
    now = datetime.now(tz=UTC)
    rows = [
        _make_row(run_id=3, finish_at=now - timedelta(hours=1)),
        _make_row(run_id=1, finish_at=now - timedelta(hours=5)),
        _make_row(run_id=2, finish_at=now - timedelta(hours=3)),
    ]
    mock_query.return_value = rows

    contents = await read_tasks_recent_results("user-1")
    payload = json.loads(contents[0].content)

    run_ids = [r["run_id"] for r in payload["runs"]]
    assert run_ids == [3, 1, 2]


# ---------------------------------------------------------------------------
# error_excerpt field
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch(_PATCH, new_callable=AsyncMock)
async def test_error_excerpt_none_when_no_error(mock_query):
    """error_excerpt is None when error_message is absent."""
    mock_query.return_value = [_make_row(status="SUCCEEDED", error_message=None)]

    contents = await read_tasks_recent_results("user-1")
    payload = json.loads(contents[0].content)
    assert payload["runs"][0]["error_excerpt"] is None


@pytest.mark.asyncio
@patch(_PATCH, new_callable=AsyncMock)
async def test_error_excerpt_truncated_at_200_chars(mock_query):
    """error_excerpt is truncated to 200 characters."""
    long_error = "x" * 300
    mock_query.return_value = [_make_row(status="FAILED", error_message=long_error)]

    contents = await read_tasks_recent_results("user-1")
    payload = json.loads(contents[0].content)
    assert len(payload["runs"][0]["error_excerpt"]) == 200


@pytest.mark.asyncio
@patch(_PATCH, new_callable=AsyncMock)
async def test_error_excerpt_short_error_not_truncated(mock_query):
    """error_excerpt preserves short error messages in full."""
    mock_query.return_value = [_make_row(status="FAILED", error_message="timeout")]

    contents = await read_tasks_recent_results("user-1")
    payload = json.loads(contents[0].content)
    assert payload["runs"][0]["error_excerpt"] == "timeout"


# ---------------------------------------------------------------------------
# cutoff: 24h window is applied to the query
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch(_PATCH, new_callable=AsyncMock)
async def test_cutoff_is_24h_before_queried_at(mock_query):
    """_query_recent_runs receives a cutoff roughly 24h before now."""
    mock_query.return_value = []

    before = datetime.now(tz=UTC)
    await read_tasks_recent_results("user-1")
    after = datetime.now(tz=UTC)

    cutoff = mock_query.call_args.kwargs["cutoff"]
    assert before - timedelta(hours=24, seconds=1) < cutoff < after - timedelta(hours=24) + timedelta(seconds=1)
