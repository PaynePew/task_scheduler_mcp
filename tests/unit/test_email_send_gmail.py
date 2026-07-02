"""Unit tests for the email_send Gmail send path (issue #140; ADR-050 amended).

Exercises:
  - Routes to the Gmail API using the caller's Google OAuth token
  - No Google connection → MISSING_CONNECTION (non-retryable)
  - Gmail API error classifications (401, 403, 429, 5xx, network)
  - Registry: email_send is public, oauth_connection, provider=google
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.actions.email_send import EmailSendHandler, EmailSendParams
from app.actions.registry import ACTION_REGISTRY
from app.connections.store import ConnectionMiss
from tests.fixtures.dedup import InMemoryDedupStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class FakeRun:
    user_id: str = "user-abc"
    run_id: int = 1


def _handler() -> EmailSendHandler:
    """EmailSendHandler wired to an in-memory dedup store (no Postgres in unit tests)."""
    return EmailSendHandler(dedup_store=InMemoryDedupStore())


def _build_mock_http_client(response: httpx.Response) -> MagicMock:
    mock_client = MagicMock()
    mock_client.post = AsyncMock(return_value=response)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_client)
    ctx.__aexit__ = AsyncMock(return_value=None)
    return ctx


def _build_capturing_http_client(captured: dict, response: httpx.Response) -> MagicMock:
    """Mock httpx client that records the posted JSON (captures the RFC 2822 raw)."""

    async def _post(url, headers=None, json=None):
        captured["json"] = json
        return response

    mock_client = MagicMock()
    mock_client.post = AsyncMock(side_effect=_post)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_client)
    ctx.__aexit__ = AsyncMock(return_value=None)
    return ctx


def _decode_sent_message(captured: dict) -> str:
    """Base64url-decode the ``raw`` field the handler POSTs to the Gmail API."""
    return base64.urlsafe_b64decode(captured["json"]["raw"]).decode()


# ---------------------------------------------------------------------------
# Registry attributes
# ---------------------------------------------------------------------------


def test_email_send_in_registry():
    assert "email_send" in ACTION_REGISTRY


def test_email_send_is_public_oauth():
    from app.actions.base import CredentialMode

    handler = ACTION_REGISTRY["email_send"]
    assert handler.requires_operator is False
    assert handler.credential_mode == CredentialMode.oauth_connection
    assert getattr(handler, "required_provider", None) == "google"


# ---------------------------------------------------------------------------
# Gmail send path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_public_user_routes_to_gmail():
    """When user is not operator, Gmail API path is used."""
    handler = _handler()
    params = EmailSendParams(to=["dest@example.com"], subject="Test", body="Body text")
    run = FakeRun(user_id="user-abc")

    success_resp = httpx.Response(200, json={"id": "msg-123", "threadId": "thread-abc"})

    with (
        patch("app.actions.email_send.settings") as mock_settings,
        patch(
            "app.actions.email_send.get_token",
            AsyncMock(return_value="ya29.fake-token"),
        ),
        patch(
            "app.actions.email_send.httpx.AsyncClient",
            return_value=_build_mock_http_client(success_resp),
        ),
    ):
        mock_settings.operator_user_id = "operator-uid"
        mock_settings.connections_base_url = "http://localhost:8000"

        result = await handler.execute(run=run, params=params)

    assert result.ok is True
    assert result.result is not None
    assert result.result.get("provider") == "gmail"
    assert "dest@example.com" in result.result.get("recipients", [])


@pytest.mark.asyncio
async def test_public_user_no_google_connection_returns_error():
    """Public user with no Google connection gets a non-retryable error."""
    handler = _handler()
    params = EmailSendParams(to=["dest@example.com"], subject="Test", body="Body")
    run = FakeRun(user_id="user-abc")

    with (
        patch("app.actions.email_send.settings") as mock_settings,
        patch(
            "app.actions.email_send.get_token",
            AsyncMock(side_effect=ConnectionMiss("user-abc", "google")),
        ),
    ):
        mock_settings.operator_user_id = "operator-uid"
        mock_settings.connections_base_url = "http://localhost:8000"

        result = await handler.execute(run=run, params=params)

    assert result.ok is False
    assert result.retryable is False
    assert "google" in (result.error or "").lower()


# ---------------------------------------------------------------------------
# Gmail API error classifications
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gmail_401_is_dlq():
    handler = _handler()
    params = EmailSendParams(to=["dest@example.com"], subject="Test", body="Body")
    run = FakeRun(user_id="user-abc")

    resp = httpx.Response(401, json={"error": "invalid_token"})

    with (
        patch("app.actions.email_send.settings") as mock_settings,
        patch(
            "app.actions.email_send.get_token",
            AsyncMock(return_value="ya29.fake-token"),
        ),
        patch(
            "app.actions.email_send.httpx.AsyncClient",
            return_value=_build_mock_http_client(resp),
        ),
    ):
        mock_settings.operator_user_id = "operator-uid"
        mock_settings.connections_base_url = "http://localhost:8000"

        result = await handler.execute(run=run, params=params)

    assert result.ok is False
    assert result.retryable is False
    assert "401" in (result.error or "")


@pytest.mark.asyncio
async def test_gmail_403_is_dlq():
    handler = _handler()
    params = EmailSendParams(to=["dest@example.com"], subject="Test", body="Body")
    run = FakeRun(user_id="user-abc")

    resp = httpx.Response(403, json={"error": "forbidden"})

    with (
        patch("app.actions.email_send.settings") as mock_settings,
        patch(
            "app.actions.email_send.get_token",
            AsyncMock(return_value="ya29.fake-token"),
        ),
        patch(
            "app.actions.email_send.httpx.AsyncClient",
            return_value=_build_mock_http_client(resp),
        ),
    ):
        mock_settings.operator_user_id = "operator-uid"
        mock_settings.connections_base_url = "http://localhost:8000"

        result = await handler.execute(run=run, params=params)

    assert result.ok is False
    assert result.retryable is False
    assert "403" in (result.error or "")


@pytest.mark.asyncio
async def test_gmail_429_is_retryable():
    handler = _handler()
    params = EmailSendParams(to=["dest@example.com"], subject="Test", body="Body")
    run = FakeRun(user_id="user-abc")

    resp = httpx.Response(429, json={"error": "rateLimitExceeded"})

    with (
        patch("app.actions.email_send.settings") as mock_settings,
        patch(
            "app.actions.email_send.get_token",
            AsyncMock(return_value="ya29.fake-token"),
        ),
        patch(
            "app.actions.email_send.httpx.AsyncClient",
            return_value=_build_mock_http_client(resp),
        ),
    ):
        mock_settings.operator_user_id = "operator-uid"
        mock_settings.connections_base_url = "http://localhost:8000"

        result = await handler.execute(run=run, params=params)

    assert result.ok is False
    assert result.retryable is True


@pytest.mark.asyncio
async def test_gmail_500_is_retryable():
    handler = _handler()
    params = EmailSendParams(to=["dest@example.com"], subject="Test", body="Body")
    run = FakeRun(user_id="user-abc")

    resp = httpx.Response(500, json={"error": "backendError"})

    with (
        patch("app.actions.email_send.settings") as mock_settings,
        patch(
            "app.actions.email_send.get_token",
            AsyncMock(return_value="ya29.fake-token"),
        ),
        patch(
            "app.actions.email_send.httpx.AsyncClient",
            return_value=_build_mock_http_client(resp),
        ),
    ):
        mock_settings.operator_user_id = "operator-uid"
        mock_settings.connections_base_url = "http://localhost:8000"

        result = await handler.execute(run=run, params=params)

    assert result.ok is False
    assert result.retryable is True


@pytest.mark.asyncio
async def test_gmail_network_error_is_retryable():
    handler = _handler()
    params = EmailSendParams(to=["dest@example.com"], subject="Test", body="Body")
    run = FakeRun(user_id="user-abc")

    mock_client = MagicMock()
    mock_client.post = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_client)
    ctx.__aexit__ = AsyncMock(return_value=None)

    with (
        patch("app.actions.email_send.settings") as mock_settings,
        patch(
            "app.actions.email_send.get_token",
            AsyncMock(return_value="ya29.fake-token"),
        ),
        patch("app.actions.email_send.httpx.AsyncClient", return_value=ctx),
    ):
        mock_settings.operator_user_id = "operator-uid"
        mock_settings.connections_base_url = "http://localhost:8000"

        result = await handler.execute(run=run, params=params)

    assert result.ok is False
    assert result.retryable is True


# ---------------------------------------------------------------------------
# Routing — Gmail-only (no operator/SMTP branch)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_routes_to_gmail_regardless_of_operator_status():
    """email_send is Gmail-only: even the operator routes to Gmail (ADR-050 amended)."""
    handler = _handler()
    params = EmailSendParams(to=["dest@example.com"], subject="Hi", body="Body")
    run = FakeRun(user_id="operator-uid")  # the operator themselves

    success_resp = httpx.Response(200, json={"id": "msg-1"})
    with (
        patch("app.actions.email_send.settings") as mock_settings,
        patch("app.actions.email_send.get_token", AsyncMock(return_value="ya29.tok")),
        patch(
            "app.actions.email_send.httpx.AsyncClient",
            return_value=_build_mock_http_client(success_resp),
        ),
    ):
        mock_settings.operator_user_id = "operator-uid"  # this user IS the operator
        mock_settings.connections_base_url = "http://localhost:8000"
        result = await handler.execute(run=run, params=params)

    assert result.ok is True
    assert result.result.get("provider") == "gmail"


# ---------------------------------------------------------------------------
# Security — no ${VAR} operator-secret substitution (public action; ADR-050/052)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_email_send_does_not_substitute_env_vars(monkeypatch):
    """email_send is PUBLIC: a ${VAR} in subject/body must reach Gmail as literal
    text, never the operator's secret (blocks secret exfiltration)."""
    monkeypatch.setenv("GITHUB_TOKEN", "super-secret-operator-value")
    handler = _handler()
    params = EmailSendParams(
        to=["dest@example.com"],
        subject="Report ${GITHUB_TOKEN}",
        body="the token is ${GITHUB_TOKEN}",
    )
    run = FakeRun(user_id="user-abc")

    captured: dict = {}
    success_resp = httpx.Response(200, json={"id": "msg-1"})
    with (
        patch("app.actions.email_send.settings") as mock_settings,
        patch("app.actions.email_send.get_token", AsyncMock(return_value="ya29.tok")),
        patch(
            "app.actions.email_send.httpx.AsyncClient",
            return_value=_build_capturing_http_client(captured, success_resp),
        ),
    ):
        mock_settings.operator_user_id = "operator-uid"
        mock_settings.connections_base_url = "http://localhost:8000"
        result = await handler.execute(run=run, params=params)

    assert result.ok is True
    sent = _decode_sent_message(captured)
    # Literal ${VAR} passes through un-substituted...
    assert "${GITHUB_TOKEN}" in sent
    # ...and the operator secret is never leaked into the outgoing message.
    assert "super-secret-operator-value" not in sent


# ---------------------------------------------------------------------------
# Security — Subject header-injection defense-in-depth (FIX #4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_email_send_subject_no_header_injection():
    """A newline + 'Bcc:' embedded in the subject must not produce a Bcc header:
    CR/LF are stripped before the subject is set, so no second header is injected."""
    handler = _handler()
    params = EmailSendParams(
        to=["dest@example.com"],
        subject="Hello\r\nBcc: attacker@evil.com",
        body="Body",
    )
    run = FakeRun(user_id="user-abc")

    captured: dict = {}
    success_resp = httpx.Response(200, json={"id": "msg-1"})
    with (
        patch("app.actions.email_send.settings") as mock_settings,
        patch("app.actions.email_send.get_token", AsyncMock(return_value="ya29.tok")),
        patch(
            "app.actions.email_send.httpx.AsyncClient",
            return_value=_build_capturing_http_client(captured, success_resp),
        ),
    ):
        mock_settings.operator_user_id = "operator-uid"
        mock_settings.connections_base_url = "http://localhost:8000"
        result = await handler.execute(run=run, params=params)

    assert result.ok is True
    sent = _decode_sent_message(captured)
    header_block = sent.split("\n\n", 1)[0]
    header_lines = header_block.split("\n")
    # No standalone Bcc header was injected by the embedded newline...
    assert not any(line.lower().startswith("bcc:") for line in header_lines)
    # ...the subject collapsed to a single sanitized Subject line (CR/LF → space).
    assert "Subject: Hello  Bcc: attacker@evil.com" in header_lines
