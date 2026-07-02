"""Unit tests for the email_send action handler (Gmail-only, no SMTP).

The Gmail send path itself is covered in test_email_send_gmail.py; this file
covers body building (direct input + from_run_id chaining) and registry wiring.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.actions.email_send import EmailSendHandler, EmailSendParams, EmailTemplate
from app.actions.registry import ACTION_REGISTRY


def _make_mock_session_factory():
    mock_session = AsyncMock()
    mock_session.begin = MagicMock(return_value=mock_session)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    mock_factory_ctx = AsyncMock()
    mock_factory_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_factory_ctx.__aexit__ = AsyncMock(return_value=None)

    return MagicMock(return_value=mock_factory_ctx)


def _make_run(user_id: str = "user-abc") -> Any:
    run = MagicMock()
    run.user_id = user_id
    return run


# ---------------------------------------------------------------------------
# Body building — direct input
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_neither_body_nor_from_run_id_fails():
    """No body and no from_run_id → non-retryable error, before any send."""
    handler = EmailSendHandler()
    params = EmailSendParams(to=["a@x.com"], subject="s")  # no body, no from_run_id

    result = await handler.execute(run=_make_run(), params=params)

    assert result.ok is False
    assert result.retryable is False
    assert "body" in (result.error or "").lower()


# ---------------------------------------------------------------------------
# Body building — from_run_id chaining (verified at _build_body)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_from_run_id_ok_uses_upstream_output_as_body():
    """from_run_id → resolve_for_display output becomes the email body."""
    with (
        patch(
            "app.actions.email_send.resolve_for_display",
            AsyncMock(return_value="Daily Digest\n\n- summary: 5 issues closed\n- prs: 2"),
        ),
        patch("app.db.engine.async_session_factory", _make_mock_session_factory()),
    ):
        handler = EmailSendHandler()
        params = EmailSendParams(
            to=["user@example.com"],
            subject="Digest",
            from_run_id=42,
            template=EmailTemplate.digest_v1,
        )
        body = await handler._build_body(params, None, user_id="user-abc")

    assert isinstance(body, str)
    assert "5 issues closed" in body


@pytest.mark.asyncio
async def test_from_run_id_upstream_error_passed_through_to_body():
    """An upstream error is surfaced in the body (resolve_for_display formats it)."""
    with (
        patch(
            "app.actions.email_send.resolve_for_display",
            AsyncMock(return_value="Upstream error: github API rate limited"),
        ),
        patch("app.db.engine.async_session_factory", _make_mock_session_factory()),
    ):
        handler = EmailSendHandler()
        params = EmailSendParams(to=["user@example.com"], subject="Alert", from_run_id=99)
        body = await handler._build_body(params, None, user_id="user-abc")

    assert isinstance(body, str)
    assert "error" in body.lower() or "rate limited" in body


@pytest.mark.asyncio
async def test_from_run_id_placeholder_body_is_still_a_string():
    """NoResult / InvalidJson upstream → placeholder string body (email still sendable)."""
    with (
        patch(
            "app.actions.email_send.resolve_for_display",
            AsyncMock(return_value="Upstream error: (no result)"),
        ),
        patch("app.db.engine.async_session_factory", _make_mock_session_factory()),
    ):
        handler = EmailSendHandler()
        params = EmailSendParams(to=["user@example.com"], subject="Digest", from_run_id=1)
        body = await handler._build_body(params, None, user_id="user-abc")

    assert isinstance(body, str)
    assert body  # non-empty placeholder


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_email_send_registered_in_registry():
    assert "email_send" in ACTION_REGISTRY
    from app.actions.email_send import EmailSendHandler as H

    assert isinstance(ACTION_REGISTRY["email_send"], H)


def test_list_actions_includes_email_send():
    """task.list_actions.v1 should expose email_send among the registered actions."""
    assert len(ACTION_REGISTRY) >= 5
    assert "email_send" in ACTION_REGISTRY
