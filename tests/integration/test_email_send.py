"""Integration tests for email_send handler — end-to-end SMTP exchange.

Uses aiosmtpd as an in-process mock SMTP server to verify the full
aiosmtplib send path without TLS (SMTP_USE_STARTTLS=false).

Run with:
    uv run pytest -m integration tests/integration/test_email_send.py
"""

from __future__ import annotations

import email
import os
import socket
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from aiosmtpd.controller import Controller

from app.actions.email_send import EmailSendHandler, EmailSendParams


def _make_run(user_id: str = "operator-id") -> Any:
    run = MagicMock()
    run.user_id = user_id
    return run


# ---------------------------------------------------------------------------
# In-process SMTP server via aiosmtpd
# ---------------------------------------------------------------------------


class _CapturingHandler:
    """aiosmtpd handler that records every message envelope."""

    def __init__(self) -> None:
        self.envelopes: list[dict[str, Any]] = []

    async def handle_DATA(self, server: Any, session: Any, envelope: Any) -> str:  # noqa: N802
        self.envelopes.append(
            {
                "mail_from": envelope.mail_from,
                "rcpt_tos": list(envelope.rcpt_tos),
                "content": envelope.content.decode("utf-8", errors="replace"),
            }
        )
        return "250 Message accepted for delivery"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def smtp_server():
    """Start a plain (no-TLS) SMTP server; yield (handler, host, port); stop on teardown."""
    handler = _CapturingHandler()
    port = _free_port()
    controller = Controller(handler, hostname="127.0.0.1", port=port)
    controller.start()
    yield handler, "127.0.0.1", port
    controller.stop()


def _env(host: str, port: int) -> dict[str, str]:
    return {
        "SMTP_HOST": host,
        "SMTP_PORT": str(port),
        "SMTP_USER": "",
        "SMTP_PASSWORD": "",
        "EMAIL_FROM": "sender@example.com",
        "SMTP_USE_STARTTLS": "false",
    }


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_email_send_end_to_end_single_recipient(smtp_server):
    """Happy path: single recipient email reaches the mock SMTP server."""
    capturing, host, port = smtp_server
    handler = EmailSendHandler()
    params = EmailSendParams(
        to=["alice@example.com"],
        subject="Integration Test",
        body="Hello from email_send integration test",
    )

    with patch.dict(os.environ, _env(host, port)):
        result = await handler.execute(run=_make_run(), params=params)

    assert result.ok is True, f"Expected ok=True, got error={result.error}"
    assert result.result is not None
    assert "alice@example.com" in result.result["recipients"]

    # Verify the server actually received the message.
    assert len(capturing.envelopes) == 1
    env = capturing.envelopes[0]
    assert env["rcpt_tos"] == ["alice@example.com"]
    assert env["mail_from"] == "sender@example.com"
    assert "Integration Test" in env["content"]
    assert "Hello from email_send integration test" in env["content"]


@pytest.mark.integration
async def test_email_send_end_to_end_multi_recipient(smtp_server):
    """Multi-recipient: all addresses appear in the SMTP envelope."""
    capturing, host, port = smtp_server
    handler = EmailSendHandler()
    params = EmailSendParams(
        to=["alice@example.com", "bob@example.com"],
        subject="Multi Recipient",
        body="Group message",
    )

    with patch.dict(os.environ, _env(host, port)):
        result = await handler.execute(run=_make_run(), params=params)

    assert result.ok is True
    assert len(capturing.envelopes) == 1
    env = capturing.envelopes[0]
    assert set(env["rcpt_tos"]) == {"alice@example.com", "bob@example.com"}


@pytest.mark.integration
async def test_email_send_subject_appears_in_message(smtp_server):
    """Subject header is correctly set in the sent message."""
    capturing, host, port = smtp_server
    handler = EmailSendHandler()
    params = EmailSendParams(
        to=["carol@example.com"],
        subject="My Unique Subject 12345",
        body="body text",
    )

    with patch.dict(os.environ, _env(host, port)):
        result = await handler.execute(run=_make_run(), params=params)

    assert result.ok is True
    assert capturing.envelopes
    raw = capturing.envelopes[0]["content"]
    msg = email.message_from_string(raw)
    assert msg["Subject"] == "My Unique Subject 12345"
