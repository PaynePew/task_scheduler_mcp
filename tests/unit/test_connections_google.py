"""Unit tests for Google OAuth connect/callback/disconnect routes (issue #140).

Exercises:
  - /connections shows Google row with Connect button
  - /connections/google/connect redirects to Google OAuth
  - /connections/google/callback stores connection (fake OAuth)
  - /connections/google/disconnect removes connection
  - task.create with email_send + no Google connection → MISSING_CONNECTION error
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.web.connections import _create_session_token, make_routes

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_test_session_factory():
    fake_session = MagicMock()

    @asynccontextmanager
    async def _factory():
        yield fake_session

    return _factory, fake_session


def _make_app(session_factory=None):
    from starlette.applications import Starlette

    factory = session_factory or _make_test_session_factory()[0]
    routes = make_routes(factory)
    return Starlette(routes=routes)


# ---------------------------------------------------------------------------
# Dashboard shows Google row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connections_dashboard_shows_google():
    """The dashboard must include a Google row (AC1)."""
    app = _make_app()
    token = _create_session_token("user-abc")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={"session": token},
        follow_redirects=False,
    ) as client:
        with patch("app.web.connections._make_kms_envelope", return_value=None):
            resp = await client.get("/connections")

    assert resp.status_code == 200
    assert "Google" in resp.text
    assert "/connections/google/connect" in resp.text


# ---------------------------------------------------------------------------
# /connections/google/connect
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_google_connect_requires_session():
    app = _make_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as client:
        resp = await client.get("/connections/google/connect")

    assert resp.status_code == 302
    assert "login" in resp.headers["location"]


@pytest.mark.asyncio
async def test_google_connect_without_client_id_returns_503():
    app = _make_app()
    token = _create_session_token("user-abc")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={"session": token},
        follow_redirects=False,
    ) as client:
        resp = await client.get("/connections/google/connect")

    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_google_connect_with_client_id_redirects_to_google():
    app = _make_app()
    transport = httpx.ASGITransport(app=app)
    with patch("app.web.connections.settings") as mock_settings:
        mock_settings.google_client_id = "google-client-123"
        mock_settings.connections_base_url = "http://localhost:8000"
        mock_settings.web_session_secret = "dev-secret-32-bytes-long-exactly!"
        token = _create_session_token("user-abc")
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            cookies={"session": token},
            follow_redirects=False,
        ) as client:
            resp = await client.get("/connections/google/connect")

    assert resp.status_code == 302
    location = resp.headers["location"]
    assert "accounts.google.com" in location
    assert "google-client-123" in location
    assert "gmail.send" in location
    assert "offline" in location


# ---------------------------------------------------------------------------
# /connections/google/callback (fake OAuth callback stores connection)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_google_callback_stores_connection():
    """Simulates the Google OAuth callback with a fake code — AC2 smoke test."""
    factory, fake_session = _make_test_session_factory()
    app = _make_app(session_factory=factory)
    token = _create_session_token("user-abc")
    transport = httpx.ASGITransport(app=app)

    fake_token_data = {
        "access_token": "ya29.fake-access-token",
        "refresh_token": "1//fake-refresh-token",
        "expires_in": 3600,
        "token_type": "Bearer",
    }
    fake_envelope = MagicMock()

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={"session": token},
        follow_redirects=False,
    ) as client:
        with (
            patch("app.web.connections._make_kms_envelope", return_value=fake_envelope),
            patch(
                "app.web.connections._exchange_google_code",
                new=AsyncMock(return_value=fake_token_data),
            ),
            patch("app.web.connections.ConnectionStore") as mock_store_class,
        ):
            mock_store = AsyncMock()
            mock_store.upsert = AsyncMock()
            mock_store_class.return_value = mock_store
            fake_session.commit = AsyncMock()

            resp = await client.get(
                "/connections/google/callback", params={"code": "fake-code", "state": ""}
            )

    assert resp.status_code == 302
    assert resp.headers["location"] == "/connections"
    mock_store.upsert.assert_called_once_with("user-abc", "google", fake_token_data)


@pytest.mark.asyncio
async def test_google_callback_requires_session():
    app = _make_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as client:
        resp = await client.get("/connections/google/callback", params={"code": "fake-code"})

    assert resp.status_code == 302
    assert "login" in resp.headers["location"]


# ---------------------------------------------------------------------------
# /connections/google/disconnect
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_google_disconnect_removes_connection():
    factory, fake_session = _make_test_session_factory()
    app = _make_app(session_factory=factory)
    token = _create_session_token("user-abc")
    transport = httpx.ASGITransport(app=app)
    fake_envelope = MagicMock()

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={"session": token},
        follow_redirects=False,
    ) as client:
        with (
            patch("app.web.connections._make_kms_envelope", return_value=fake_envelope),
            patch("app.web.connections.ConnectionStore") as mock_store_class,
        ):
            mock_store = AsyncMock()
            mock_store.get_fresh_token = AsyncMock(return_value="ya29.fake-token")
            mock_store.delete = AsyncMock()
            mock_store_class.return_value = mock_store
            fake_session.commit = AsyncMock()

            with patch("app.web.connections._revoke_google_token", new=AsyncMock()):
                resp = await client.post("/connections/google/disconnect")

    assert resp.status_code == 302
    mock_store.delete.assert_called_once_with("user-abc", "google")


# ---------------------------------------------------------------------------
# task.create email_send + no Google connection → MISSING_CONNECTION
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_task_create_email_send_no_connection_returns_connect_url():
    """Public user scheduling email_send without a Google connection gets connect_url (AC5)."""
    from contextlib import asynccontextmanager

    from app.connections.store import ConnectionMiss
    from app.crypto.kms_envelope import KmsEnvelope
    from app.mcp.server import _check_oauth_connection

    fake_session = MagicMock()

    @asynccontextmanager
    async def _factory():
        yield fake_session

    fake_envelope = MagicMock(spec=KmsEnvelope)

    with (
        patch("app.mcp.server._make_server_kms_envelope", return_value=fake_envelope),
        patch("app.mcp.server.ConnectionStore") as mock_store_class,
    ):
        mock_store = AsyncMock()
        mock_store.get_fresh_token = AsyncMock(side_effect=ConnectionMiss("user-abc", "google"))
        mock_store_class.return_value = mock_store

        result = await _check_oauth_connection("email_send", "user-abc", _factory)

    assert result is not None
    assert result["ok"] is False
    assert result["error"]["code"] == "MISSING_CONNECTION"
    assert "connect_url" in result["error"]
    assert "/connections" in result["error"]["connect_url"]
