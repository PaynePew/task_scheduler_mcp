"""Unit tests for the email_send Gmail send path (issue #140; ADR-050 amended).

Exercises:
  - Routes to the Gmail API using the caller's Google OAuth token
  - No Google connection → MISSING_CONNECTION (non-retryable)
  - Gmail API error classifications (401, 403, 429, 5xx, network)
  - Registry: email_send is public, oauth_connection, provider=google
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.actions.email_send import EmailSendHandler, EmailSendParams
from app.actions.registry import ACTION_REGISTRY
from app.connections.store import ConnectionMiss

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class FakeRun:
    user_id: str = "user-abc"


def _build_mock_http_client(response: httpx.Response) -> MagicMock:
    mock_client = MagicMock()
    mock_client.post = AsyncMock(return_value=response)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_client)
    ctx.__aexit__ = AsyncMock(return_value=None)
    return ctx


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
    handler = EmailSendHandler()
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
    handler = EmailSendHandler()
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
    handler = EmailSendHandler()
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
    handler = EmailSendHandler()
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
    handler = EmailSendHandler()
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
    handler = EmailSendHandler()
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
    handler = EmailSendHandler()
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
    handler = EmailSendHandler()
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
