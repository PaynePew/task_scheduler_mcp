"""Unit tests: task.status.v1 surfaces error_message even without an error_code.

Failures that carry a message but no structured code — param validation,
SMTP-config, generic ActionResult errors — were previously invisible via the
API (the message lived only in job_runs.error_message). These tests pin the
fix at app/mcp/handlers/status.py without requiring Postgres.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.mcp.handlers import status as status_handler
from app.mcp.handlers.status import handle_task_status


def _mock_session_factory():
    """A session_factory whose factory() is an async context manager."""
    session = AsyncMock()
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=None)
    return MagicMock(return_value=ctx)


def _view(**overrides):
    base = dict(
        job_id=42,
        action="email_send",
        job_state="completed",
        latest_run_status="FAILED",
        triggered_by=None,
        error_code=None,
        error_message=None,
        runs=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_surfaces_error_message_without_error_code(monkeypatch):
    """A failed run with a message but no error_code → error.message is returned."""
    view = _view(error_message="invalid params: 'to' must be a list")
    monkeypatch.setattr(status_handler, "get_job_with_runs", AsyncMock(return_value=view))

    result = await handle_task_status(
        {"job_id": 42}, user_id="u1", session_factory=_mock_session_factory()
    )

    assert result["ok"] is True
    err = result["data"]["error"]
    assert err["message"] == "invalid params: 'to' must be a list"
    assert "code" not in err
    assert "connect_url" not in err


@pytest.mark.asyncio
async def test_missing_connection_still_includes_code_and_connect_url(monkeypatch):
    """Regression: the MISSING_CONNECTION shape (code + connect_url) is preserved."""
    view = _view(
        error_code="MISSING_CONNECTION",
        error_message="Your Google connection has expired or been revoked.",
    )
    monkeypatch.setattr(status_handler, "get_job_with_runs", AsyncMock(return_value=view))

    result = await handle_task_status(
        {"job_id": 7}, user_id="u1", session_factory=_mock_session_factory()
    )

    err = result["data"]["error"]
    assert err["code"] == "MISSING_CONNECTION"
    assert err["message"].startswith("Your Google")
    assert "/connections" in err["connect_url"]


@pytest.mark.asyncio
async def test_no_error_block_when_run_succeeded(monkeypatch):
    """A run with neither code nor message → no 'error' key in the payload."""
    view = _view(latest_run_status="SUCCEEDED")
    monkeypatch.setattr(status_handler, "get_job_with_runs", AsyncMock(return_value=view))

    result = await handle_task_status(
        {"job_id": 9}, user_id="u1", session_factory=_mock_session_factory()
    )

    assert result["ok"] is True
    assert "error" not in result["data"]
