"""Unit tests for the email_send action handler (no real network or DB)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiosmtplib
import pytest

from app.actions.email_send import EmailSendHandler, EmailSendParams, EmailTemplate
from app.actions.registry import ACTION_REGISTRY
from app.chain.upstream_reader import InvalidJson, NoResult, Ok, UpstreamError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SMTP_ENV = {
    "SMTP_HOST": "smtp.example.com",
    "SMTP_PORT": "587",
    "SMTP_USER": "user@example.com",
    "SMTP_PASSWORD": "secret",
    "EMAIL_FROM": "noreply@example.com",
}


def _patch_smtp_send(side_effect=None):
    """Patch aiosmtplib.send to succeed (or raise side_effect)."""
    if side_effect is not None:
        return patch("app.actions.email_send.aiosmtplib.send", AsyncMock(side_effect=side_effect))
    return patch(
        "app.actions.email_send.aiosmtplib.send",
        AsyncMock(return_value=({}, "250 OK")),
    )


def _make_mock_session_factory() -> Any:
    """Return a mock async_session_factory suitable for from_run_id tests."""
    mock_session = AsyncMock()
    mock_session.begin = MagicMock(return_value=mock_session)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    mock_factory_ctx = AsyncMock()
    mock_factory_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_factory_ctx.__aexit__ = AsyncMock(return_value=None)

    return MagicMock(return_value=mock_factory_ctx)


# ---------------------------------------------------------------------------
# Happy path — single and multi-recipient
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_email_send_single_recipient_ok():
    handler = EmailSendHandler()
    params = EmailSendParams(to=["alice@example.com"], subject="Hello", body="World")

    with patch.dict("os.environ", _SMTP_ENV):
        with _patch_smtp_send():
            result = await handler.execute(run=None, params=params)

    assert result.ok is True
    assert result.error is None
    assert result.retryable is False
    assert result.result is not None
    assert "alice@example.com" in result.result["recipients"]


@pytest.mark.asyncio
async def test_email_send_multi_recipient_ok():
    handler = EmailSendHandler()
    params = EmailSendParams(
        to=["alice@example.com", "bob@example.com"],
        subject="Digest",
        body="Daily summary",
    )

    with patch.dict("os.environ", _SMTP_ENV):
        with _patch_smtp_send():
            result = await handler.execute(run=None, params=params)

    assert result.ok is True
    assert "alice@example.com" in result.result["recipients"]
    assert "bob@example.com" in result.result["recipients"]


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_smtp_4xx_recipient_is_retryable():
    """SMTP 4xx temporary rejection → retryable=True."""
    handler = EmailSendHandler()
    params = EmailSendParams(to=["a@x.com"], subject="s", body="b")

    refused = [aiosmtplib.SMTPRecipientRefused(452, "Mailbox full", "a@x.com")]
    exc = aiosmtplib.SMTPRecipientsRefused(refused)

    with patch.dict("os.environ", _SMTP_ENV):
        with _patch_smtp_send(side_effect=exc):
            result = await handler.execute(run=None, params=params)

    assert result.ok is False
    assert result.retryable is True


@pytest.mark.asyncio
async def test_smtp_5xx_recipient_not_retryable():
    """SMTP 5xx permanent rejection → retryable=False → DLQ."""
    handler = EmailSendHandler()
    params = EmailSendParams(to=["a@x.com"], subject="s", body="b")

    refused = [aiosmtplib.SMTPRecipientRefused(550, "No such user", "a@x.com")]
    exc = aiosmtplib.SMTPRecipientsRefused(refused)

    with patch.dict("os.environ", _SMTP_ENV):
        with _patch_smtp_send(side_effect=exc):
            result = await handler.execute(run=None, params=params)

    assert result.ok is False
    assert result.retryable is False


@pytest.mark.asyncio
async def test_smtp_auth_failure_not_retryable():
    """SMTP 5.7.0 auth failure → retryable=False → DLQ + operator action."""
    handler = EmailSendHandler()
    params = EmailSendParams(to=["a@x.com"], subject="s", body="b")

    exc = aiosmtplib.SMTPAuthenticationError(535, "5.7.0 Authentication credentials invalid")

    with patch.dict("os.environ", _SMTP_ENV):
        with _patch_smtp_send(side_effect=exc):
            result = await handler.execute(run=None, params=params)

    assert result.ok is False
    assert result.retryable is False
    assert "auth" in (result.error or "").lower()


@pytest.mark.asyncio
async def test_smtp_tls_handshake_failure_is_retryable():
    """TLS handshake failure → retryable=True."""
    handler = EmailSendHandler()
    params = EmailSendParams(to=["a@x.com"], subject="s", body="b")

    exc = aiosmtplib.SMTPConnectError("TLS handshake failed")

    with patch.dict("os.environ", _SMTP_ENV):
        with _patch_smtp_send(side_effect=exc):
            result = await handler.execute(run=None, params=params)

    assert result.ok is False
    assert result.retryable is True


@pytest.mark.asyncio
async def test_smtp_connect_timeout_is_retryable():
    """Connection timeout → retryable=True."""
    handler = EmailSendHandler()
    params = EmailSendParams(to=["a@x.com"], subject="s", body="b")

    exc = aiosmtplib.SMTPConnectTimeoutError("Connection timed out")

    with patch.dict("os.environ", _SMTP_ENV):
        with _patch_smtp_send(side_effect=exc):
            result = await handler.execute(run=None, params=params)

    assert result.ok is False
    assert result.retryable is True


@pytest.mark.asyncio
async def test_smtp_server_disconnected_is_retryable():
    """Server disconnect → retryable=True."""
    handler = EmailSendHandler()
    params = EmailSendParams(to=["a@x.com"], subject="s", body="b")

    exc = aiosmtplib.SMTPServerDisconnected("Server closed connection unexpectedly")

    with patch.dict("os.environ", _SMTP_ENV):
        with _patch_smtp_send(side_effect=exc):
            result = await handler.execute(run=None, params=params)

    assert result.ok is False
    assert result.retryable is True


# ---------------------------------------------------------------------------
# Missing env vars
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_smtp_host_fails():
    handler = EmailSendHandler()
    params = EmailSendParams(to=["a@x.com"], subject="s", body="b")

    env = {k: v for k, v in _SMTP_ENV.items() if k != "SMTP_HOST"}
    with patch.dict("os.environ", env, clear=True):
        result = await handler.execute(run=None, params=params)

    assert result.ok is False
    assert result.retryable is False
    assert "SMTP_HOST" in (result.error or "")


@pytest.mark.asyncio
async def test_missing_email_from_fails():
    handler = EmailSendHandler()
    params = EmailSendParams(to=["a@x.com"], subject="s", body="b")

    env = {k: v for k, v in _SMTP_ENV.items() if k != "EMAIL_FROM"}
    with patch.dict("os.environ", env, clear=True):
        result = await handler.execute(run=None, params=params)

    assert result.ok is False
    assert result.retryable is False
    assert "EMAIL_FROM" in (result.error or "")


@pytest.mark.asyncio
async def test_neither_body_nor_from_run_id_fails():
    handler = EmailSendHandler()
    params = EmailSendParams(to=["a@x.com"], subject="s")  # no body, no from_run_id

    with patch.dict("os.environ", _SMTP_ENV):
        result = await handler.execute(run=None, params=params)

    assert result.ok is False
    assert result.retryable is False


# ---------------------------------------------------------------------------
# from_run_id — upstream variants
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_from_run_id_ok_uses_upstream_json_as_body():
    """from_run_id with Ok variant → email body contains upstream JSON data."""
    data = {"summary": "5 issues closed", "prs": 2}
    mock_factory = _make_mock_session_factory()

    with patch("app.actions.email_send.read_upstream", AsyncMock(return_value=Ok(data=data))):
        with patch.dict("os.environ", _SMTP_ENV):
            captured: list[Any] = []

            async def fake_send(msg, **kwargs):
                captured.append(msg)
                return {}, "250 OK"

            with patch("app.actions.email_send.aiosmtplib.send", fake_send):
                handler = EmailSendHandler(session_factory=mock_factory)
                params = EmailSendParams(
                    to=["user@example.com"],
                    subject="Digest",
                    from_run_id=42,
                    template=EmailTemplate.digest_v1,
                )
                result = await handler.execute(run=None, params=params)

    assert result.ok is True
    assert captured
    body = captured[0].get_content()
    assert "5 issues closed" in body


@pytest.mark.asyncio
async def test_from_run_id_upstream_error_alerts_in_body():
    """from_run_id with UpstreamError variant → email body contains error alert."""
    payload = UpstreamError(error_msg="github API rate limited")
    mock_factory = _make_mock_session_factory()

    with patch("app.actions.email_send.read_upstream", AsyncMock(return_value=payload)):
        with patch.dict("os.environ", _SMTP_ENV):
            captured: list[Any] = []

            async def fake_send(msg, **kwargs):
                captured.append(msg)
                return {}, "250 OK"

            with patch("app.actions.email_send.aiosmtplib.send", fake_send):
                handler = EmailSendHandler(session_factory=mock_factory)
                params = EmailSendParams(
                    to=["user@example.com"],
                    subject="Alert",
                    from_run_id=99,
                )
                result = await handler.execute(run=None, params=params)

    assert result.ok is True
    assert captured
    body = captured[0].get_content()
    assert "error" in body.lower() or "rate limited" in body


@pytest.mark.asyncio
async def test_from_run_id_no_result():
    """from_run_id with NoResult variant → email is still sent with placeholder body."""
    mock_factory = _make_mock_session_factory()

    with patch("app.actions.email_send.read_upstream", AsyncMock(return_value=NoResult())):
        with patch.dict("os.environ", _SMTP_ENV):
            with _patch_smtp_send():
                handler = EmailSendHandler(session_factory=mock_factory)
                params = EmailSendParams(to=["user@example.com"], subject="Digest", from_run_id=1)
                result = await handler.execute(run=None, params=params)

    assert result.ok is True


@pytest.mark.asyncio
async def test_from_run_id_invalid_json():
    """from_run_id with InvalidJson variant → email is still sent with placeholder body."""
    mock_factory = _make_mock_session_factory()

    with patch(
        "app.actions.email_send.read_upstream",
        AsyncMock(return_value=InvalidJson(raw='{"bad": }')),
    ):
        with patch.dict("os.environ", _SMTP_ENV):
            with _patch_smtp_send():
                handler = EmailSendHandler(session_factory=mock_factory)
                params = EmailSendParams(to=["user@example.com"], subject="Digest", from_run_id=1)
                result = await handler.execute(run=None, params=params)

    assert result.ok is True


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
