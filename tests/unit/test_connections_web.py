"""Unit tests for the /connections web dashboard and OAuth routes (ADR-058).

Exercises:
  - session cookie creation / validation
  - /connections requires auth → redirect to /connections/login
  - trust-only login sets session cookie
  - GitHub OAuth connect redirects to GitHub
  - GitHub OAuth callback stores a connection (via patched ConnectionStore)
  - GitHub disconnect removes connection
  - task.create with github_digest + no connection → MISSING_CONNECTION error with connect_url
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.web.connections import _create_session_token, _decode_session_token, make_routes

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_test_session_factory():
    """Return a fake async_sessionmaker with a MagicMock session."""
    fake_session = MagicMock()

    @asynccontextmanager
    async def _factory():
        yield fake_session

    return _factory, fake_session


def _make_app(session_factory=None):
    """Build a Starlette ASGI app with the connections routes for testing."""
    from starlette.applications import Starlette

    factory = session_factory or _make_test_session_factory()[0]
    routes = make_routes(factory)
    return Starlette(routes=routes)


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------


def test_session_token_round_trip():
    token = _create_session_token("user-xyz")
    assert isinstance(token, str)
    assert len(token) > 10
    user_id = _decode_session_token(token)
    assert user_id == "user-xyz"


def test_decode_invalid_token_returns_none():
    assert _decode_session_token("not-a-valid-jwt") is None


def test_decode_tampered_token_returns_none():
    token = _create_session_token("user-xyz")
    # Tamper with the signature
    tampered = token[:-5] + "XXXXX"
    assert _decode_session_token(tampered) is None


# ---------------------------------------------------------------------------
# /connections requires auth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connections_unauthenticated_redirects_to_login():
    app = _make_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as client:
        resp = await client.get("/connections")

    assert resp.status_code == 302
    assert resp.headers["location"] == "/connections/login"


@pytest.mark.asyncio
async def test_connections_with_valid_session_returns_200():
    app = _make_app()
    token = _create_session_token("user-abc")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={"session": token},
        follow_redirects=False,
    ) as client:
        # KMS is not configured in tests, so the dashboard shows empty connections
        with patch("app.web.connections._make_kms_envelope", return_value=None):
            resp = await client.get("/connections")

    assert resp.status_code == 200
    assert "GitHub" in resp.text
    assert "user-abc" in resp.text


# ---------------------------------------------------------------------------
# /connections/login trust-only mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_login_trust_only_sets_session_and_redirects():
    """In dev/trust-only mode (no WorkOS client config), /connections/login
    sets a session cookie and redirects to /connections."""
    app = _make_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as client:
        # workos_client_id is None by default in test settings
        resp = await client.get("/connections/login")

    assert resp.status_code == 302
    assert resp.headers["location"] == "/connections"
    assert "session" in resp.cookies


# ---------------------------------------------------------------------------
# /connections/github/connect
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_github_connect_requires_session():
    app = _make_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as client:
        resp = await client.get("/connections/github/connect")

    assert resp.status_code == 302
    assert "login" in resp.headers["location"]


@pytest.mark.asyncio
async def test_github_connect_without_client_id_returns_503():
    """When GITHUB_CLIENT_ID is not set, the connect endpoint returns 503."""
    app = _make_app()
    token = _create_session_token("user-abc")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={"session": token},
        follow_redirects=False,
    ) as client:
        # github_client_id is None by default
        resp = await client.get("/connections/github/connect")

    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_github_connect_with_client_id_redirects_to_github():
    """When GITHUB_CLIENT_ID is set, /connect redirects to github.com."""
    app = _make_app()
    transport = httpx.ASGITransport(app=app)
    # Create token and make request inside the same settings patch so the session
    # cookie is signed with the same secret used for validation.
    with patch("app.web.connections.settings") as mock_settings:
        mock_settings.github_client_id = "gh-client-123"
        mock_settings.connections_base_url = "http://localhost:8000"
        mock_settings.web_session_secret = "dev-secret-32-bytes-long-exactly!"
        token = _create_session_token("user-abc")

    with patch("app.web.connections.settings") as mock_settings:
        mock_settings.github_client_id = "gh-client-123"
        mock_settings.connections_base_url = "http://localhost:8000"
        mock_settings.web_session_secret = "dev-secret-32-bytes-long-exactly!"
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            cookies={"session": token},
            follow_redirects=False,
        ) as client:
            resp = await client.get("/connections/github/connect")

    assert resp.status_code == 302
    assert "github.com/login/oauth/authorize" in resp.headers["location"]
    assert "gh-client-123" in resp.headers["location"]


# ---------------------------------------------------------------------------
# /connections/github/callback (fake OAuth callback stores connection)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_github_callback_stores_connection():
    """Simulates the GitHub OAuth callback with a fake code.

    This is the 'fake OAuth callback stores a connection' smoke test from AC7.
    The real GitHub token exchange is mocked so no network calls are made.
    """
    factory, fake_session = _make_test_session_factory()
    app = _make_app(session_factory=factory)
    token = _create_session_token("user-abc")
    transport = httpx.ASGITransport(app=app)

    fake_token_data = {"access_token": "ghs_fake_token", "scope": "repo"}
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
                "app.web.connections._exchange_github_code",
                new=AsyncMock(return_value=fake_token_data),
            ),
            patch("app.web.connections.ConnectionStore") as mock_store_class,
        ):
            mock_store = AsyncMock()
            mock_store.upsert = AsyncMock()
            mock_store_class.return_value = mock_store
            fake_session.commit = AsyncMock()

            resp = await client.get(
                "/connections/github/callback", params={"code": "fake-code", "state": ""}
            )

    assert resp.status_code == 302
    assert resp.headers["location"] == "/connections"
    mock_store.upsert.assert_called_once_with("user-abc", "github", fake_token_data)


@pytest.mark.asyncio
async def test_github_callback_requires_session():
    app = _make_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as client:
        resp = await client.get("/connections/github/callback", params={"code": "fake-code"})

    assert resp.status_code == 302
    assert "login" in resp.headers["location"]


# ---------------------------------------------------------------------------
# /connections/github/disconnect
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_github_disconnect_removes_connection():
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
            mock_store.get_fresh_token = AsyncMock(return_value="ghs_token")
            mock_store.delete = AsyncMock()
            mock_store_class.return_value = mock_store
            fake_session.commit = AsyncMock()

            # GitHub client id not set, so no revocation call
            resp = await client.post("/connections/github/disconnect")

    assert resp.status_code == 302
    mock_store.delete.assert_called_once_with("user-abc", "github")


# ---------------------------------------------------------------------------
# task.create connect_url (MISSING_CONNECTION envelope field)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_task_create_github_digest_no_connection_returns_connect_url():
    """When user has no GitHub connection, task.create returns MISSING_CONNECTION
    with a connect_url field (AC5 from issue #138)."""
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock, MagicMock

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
        mock_store.get_fresh_token = AsyncMock(side_effect=ConnectionMiss("user-abc", "github"))
        mock_store_class.return_value = mock_store

        result = await _check_oauth_connection("github_digest", "user-abc", _factory)

    assert result is not None
    assert result["ok"] is False
    assert result["error"]["code"] == "MISSING_CONNECTION"
    assert "connect_url" in result["error"]
    assert "/connections" in result["error"]["connect_url"]


@pytest.mark.asyncio
async def test_task_create_non_oauth_action_skips_connection_check():
    """echo action (no oauth_connection) skips the connection check."""
    from contextlib import asynccontextmanager

    from app.mcp.server import _check_oauth_connection

    @asynccontextmanager
    async def _factory():
        yield MagicMock()

    result = await _check_oauth_connection("echo", "user-abc", _factory)
    assert result is None


@pytest.mark.asyncio
async def test_task_create_no_kms_skips_connection_check():
    """When KMS is not configured, the connection check is skipped."""
    from contextlib import asynccontextmanager

    from app.mcp.server import _check_oauth_connection

    @asynccontextmanager
    async def _factory():
        yield MagicMock()

    with patch("app.mcp.server._make_server_kms_envelope", return_value=None):
        result = await _check_oauth_connection("github_digest", "user-abc", _factory)

    assert result is None
