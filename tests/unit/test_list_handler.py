"""Unit tests for task.list@v1 handler — validation logic (no DB)."""

from __future__ import annotations

import pytest

from app.mcp.handlers.list import handle_task_list

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _call(args: dict) -> dict:
    """Call handle_task_list without a real DB (only validation paths tested here)."""
    return await handle_task_list(args, user_id="test-user")


# ---------------------------------------------------------------------------
# Pagination validation
# ---------------------------------------------------------------------------


async def test_page_less_than_1_returns_user_input() -> None:
    result = await _call({"page": 0})
    assert result["ok"] is False
    assert result["error"]["code"] == "USER_INPUT"
    assert result["error"]["field"] == "page"


async def test_page_negative_returns_user_input() -> None:
    result = await _call({"page": -5})
    assert result["ok"] is False
    assert result["error"]["code"] == "USER_INPUT"
    assert result["error"]["field"] == "page"


async def test_page_size_less_than_1_returns_user_input() -> None:
    result = await _call({"pageSize": 0})
    assert result["ok"] is False
    assert result["error"]["code"] == "USER_INPUT"
    assert result["error"]["field"] == "pageSize"
    assert result["error"]["expected"] == "1..100"


async def test_page_size_greater_than_100_returns_user_input() -> None:
    result = await _call({"pageSize": 101})
    assert result["ok"] is False
    assert result["error"]["code"] == "USER_INPUT"
    assert result["error"]["field"] == "pageSize"
    assert result["error"]["expected"] == "1..100"


async def test_page_size_exactly_100_is_valid() -> None:
    # Validation passes; will get INTERNAL (no DB) — that's expected.
    result = await _call({"pageSize": 100})
    assert not (result.get("ok") is False and result.get("error", {}).get("field") == "pageSize")


async def test_page_size_exactly_1_is_valid() -> None:
    result = await _call({"pageSize": 1})
    assert not (result.get("ok") is False and result.get("error", {}).get("field") == "pageSize")


# ---------------------------------------------------------------------------
# Status filter validation
# ---------------------------------------------------------------------------


async def test_invalid_status_returns_user_input() -> None:
    result = await _call({"status": "bogus"})
    assert result["ok"] is False
    assert result["error"]["code"] == "USER_INPUT"
    assert result["error"]["field"] == "status"


@pytest.mark.parametrize("status", ["scheduled", "running", "completed", "failed", "cancelled"])
async def test_valid_statuses_pass_validation(status: str) -> None:
    result = await _call({"status": status})
    assert not (result.get("ok") is False and result.get("error", {}).get("field") == "status")


# ---------------------------------------------------------------------------
# created_at range validation
# ---------------------------------------------------------------------------


async def test_created_at_from_invalid_string_returns_user_input() -> None:
    result = await _call({"created_at_from": "not-a-date"})
    assert result["ok"] is False
    assert result["error"]["code"] == "USER_INPUT"
    assert result["error"]["field"] == "created_at_from"


async def test_created_at_from_naive_datetime_returns_user_input() -> None:
    result = await _call({"created_at_from": "2026-01-01T00:00:00"})
    assert result["ok"] is False
    assert result["error"]["code"] == "USER_INPUT"
    assert result["error"]["field"] == "created_at_from"


async def test_created_at_to_invalid_string_returns_user_input() -> None:
    result = await _call({"created_at_to": "not-a-date"})
    assert result["ok"] is False
    assert result["error"]["code"] == "USER_INPUT"
    assert result["error"]["field"] == "created_at_to"


async def test_created_at_to_naive_datetime_returns_user_input() -> None:
    result = await _call({"created_at_to": "2026-01-01T00:00:00"})
    assert result["ok"] is False
    assert result["error"]["code"] == "USER_INPUT"
    assert result["error"]["field"] == "created_at_to"


async def test_created_at_from_tz_aware_passes_validation() -> None:
    result = await _call({"created_at_from": "2026-01-01T00:00:00+00:00"})
    assert not (
        result.get("ok") is False and result.get("error", {}).get("field") == "created_at_from"
    )


async def test_created_at_to_tz_aware_passes_validation() -> None:
    result = await _call({"created_at_to": "2026-01-01T00:00:00Z"})
    assert not (
        result.get("ok") is False and result.get("error", {}).get("field") == "created_at_to"
    )
