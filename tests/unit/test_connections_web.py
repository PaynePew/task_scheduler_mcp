"""Unit tests for the /connections web dashboard and OAuth routes (ADR-058).

Exercises:
  - session cookie creation / validation
  - /connections requires auth → redirect to /connections/login
  - trust-only login sets session cookie
  - GitHub OAuth connect redirects to GitHub
  - GitHub OAuth callback stores a connection (via patched ConnectionStore)
  - GitHub disconnect removes connection
  - task.create with github_digest + no connection → MISSING_CONNECTION error with connect_url
  - WorkOS id_token fallback: tampered signature rejected, valid signature accepted
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import jwt
import pytest
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.asymmetric import rsa

from app.web.connections import _create_session_token, _decode_session_token, make_routes

# ---------------------------------------------------------------------------
# WorkOS id_token test fixtures (RSA key pair)
# ---------------------------------------------------------------------------

_WORKOS_PRIVATE_KEY = rsa.generate_private_key(
    public_exponent=65537,
    key_size=2048,
    backend=default_backend(),
)
_WORKOS_PUBLIC_KEY = _WORKOS_PRIVATE_KEY.public_key()
_WORKOS_ISSUER = "https://api.workos.test"
_WORKOS_CLIENT_ID = "workos-client-test-123"
_WORKOS_SUB = "user_workos_01HXYZ"


def _make_workos_jwks_client(key=_WORKOS_PUBLIC_KEY) -> MagicMock:
    signing_key = MagicMock()
    signing_key.key = key
    client = MagicMock()
    client.get_signing_key_from_jwt.return_value = signing_key
    return client


def _make_workos_id_token(
    *,
    private_key=_WORKOS_PRIVATE_KEY,
    sub: str = _WORKOS_SUB,
    aud: str = _WORKOS_CLIENT_ID,
    iss: str = _WORKOS_ISSUER,
    exp_offset: int = 3600,
) -> str:
    now = int(time.time())
    payload = {
        "sub": sub,
        "aud": aud,
        "iss": iss,
        "iat": now,
        "exp": now + exp_offset,
    }
    return jwt.encode(payload, private_key, algorithm="RS256")


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
    # Sign the session cookie and serve the request under the same patched
    # settings so the secret used to mint the token matches the one used to
    # validate it.
    with patch("app.web.connections.settings") as mock_settings:
        mock_settings.github_client_id = "gh-client-123"
        mock_settings.connections_base_url = "http://localhost:8000"
        mock_settings.web_session_secret = "dev-secret-32-bytes-long-exactly!"
        token = _create_session_token("user-abc")
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


# ---------------------------------------------------------------------------
# /connections dashboard shows Slack row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connections_dashboard_shows_slack():
    """Authenticated user sees a Slack row in the connections dashboard."""
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
    assert "Slack" in resp.text
    assert "GitHub" in resp.text


# ---------------------------------------------------------------------------
# /connections/slack/connect
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slack_connect_requires_session():
    app = _make_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as client:
        resp = await client.get("/connections/slack/connect")

    assert resp.status_code == 302
    assert "login" in resp.headers["location"]


@pytest.mark.asyncio
async def test_slack_connect_without_client_id_returns_503():
    """When SLACK_CLIENT_ID is not set, the connect endpoint returns 503."""
    app = _make_app()
    token = _create_session_token("user-abc")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={"session": token},
        follow_redirects=False,
    ) as client:
        # slack_client_id is None by default
        resp = await client.get("/connections/slack/connect")

    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_slack_connect_with_client_id_redirects_to_slack():
    """When SLACK_CLIENT_ID is set, /connect redirects to slack.com."""
    app = _make_app()
    transport = httpx.ASGITransport(app=app)
    with patch("app.web.connections.settings") as mock_settings:
        mock_settings.slack_client_id = "slack-client-123"
        mock_settings.connections_base_url = "http://localhost:8000"
        mock_settings.web_session_secret = "dev-secret-32-bytes-long-exactly!"
        token = _create_session_token("user-abc")
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            cookies={"session": token},
            follow_redirects=False,
        ) as client:
            resp = await client.get("/connections/slack/connect")

    assert resp.status_code == 302
    assert "slack.com/oauth/v2/authorize" in resp.headers["location"]
    assert "slack-client-123" in resp.headers["location"]


# ---------------------------------------------------------------------------
# /connections/slack/callback (fake OAuth callback stores connection)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slack_callback_stores_connection():
    """Simulates the Slack OAuth callback with a fake code.

    The real Slack token exchange is mocked so no network calls are made.
    """
    factory, fake_session = _make_test_session_factory()
    app = _make_app(session_factory=factory)
    token = _create_session_token("user-abc")
    transport = httpx.ASGITransport(app=app)

    fake_token_data = {
        "ok": True,
        "access_token": "xoxb-fake-token",
        "scope": "chat:write",
        "token_type": "bot",
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
                "app.web.connections._exchange_slack_code",
                new=AsyncMock(return_value=fake_token_data),
            ),
            patch("app.web.connections.ConnectionStore") as mock_store_class,
        ):
            mock_store = AsyncMock()
            mock_store.upsert = AsyncMock()
            mock_store_class.return_value = mock_store
            fake_session.commit = AsyncMock()

            resp = await client.get(
                "/connections/slack/callback", params={"code": "fake-code", "state": ""}
            )

    assert resp.status_code == 302
    assert resp.headers["location"] == "/connections"
    mock_store.upsert.assert_called_once_with("user-abc", "slack", fake_token_data)


@pytest.mark.asyncio
async def test_slack_callback_requires_session():
    app = _make_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as client:
        resp = await client.get("/connections/slack/callback", params={"code": "fake-code"})

    assert resp.status_code == 302
    assert "login" in resp.headers["location"]


# ---------------------------------------------------------------------------
# /connections/slack/disconnect
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slack_disconnect_removes_connection():
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
            mock_store.get_fresh_token = AsyncMock(return_value="xoxb-token")
            mock_store.delete = AsyncMock()
            mock_store_class.return_value = mock_store
            fake_session.commit = AsyncMock()

            resp = await client.post("/connections/slack/disconnect")

    assert resp.status_code == 302
    mock_store.delete.assert_called_once_with("user-abc", "slack")


# ---------------------------------------------------------------------------
# task.create with slack_post + no Slack connection → connect_url
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_task_create_slack_post_no_connection_returns_connect_url():
    """When user has no Slack connection, task.create returns MISSING_CONNECTION
    with a connect_url field."""
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
        mock_store.get_fresh_token = AsyncMock(side_effect=ConnectionMiss("user-abc", "slack"))
        mock_store_class.return_value = mock_store

        result = await _check_oauth_connection("slack_post", "user-abc", _factory)

    assert result is not None
    assert result["ok"] is False
    assert result["error"]["code"] == "MISSING_CONNECTION"
    assert "connect_url" in result["error"]
    assert "/connections" in result["error"]["connect_url"]


# ---------------------------------------------------------------------------
# WorkOS id_token fallback: signature verification (issue #160)
# ---------------------------------------------------------------------------


def test_verify_id_token_sub_valid_token_accepted():
    """_verify_id_token_sub returns sub when token is properly signed."""
    from app.web.connections import _verify_id_token_sub

    id_token = _make_workos_id_token()
    sub = _verify_id_token_sub(
        id_token,
        issuer=_WORKOS_ISSUER,
        client_id=_WORKOS_CLIENT_ID,
        jwks_uri="https://unused.test/jwks",
        _jwks_client=_make_workos_jwks_client(),
    )
    assert sub == _WORKOS_SUB


def test_verify_id_token_sub_tampered_signature_raises():
    """_verify_id_token_sub raises TokenValidationError for wrong signature."""
    from app.auth.token_validation import TokenValidationError
    from app.web.connections import _verify_id_token_sub

    other_private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
        backend=default_backend(),
    )
    # Token signed by a *different* key than what the mock JWKS client returns
    id_token = _make_workos_id_token(private_key=other_private_key)

    with pytest.raises(TokenValidationError):
        _verify_id_token_sub(
            id_token,
            issuer=_WORKOS_ISSUER,
            client_id=_WORKOS_CLIENT_ID,
            jwks_uri="https://unused.test/jwks",
            _jwks_client=_make_workos_jwks_client(key=_WORKOS_PUBLIC_KEY),
        )


@pytest.mark.asyncio
async def test_workos_callback_valid_id_token_sets_session():
    """When access_token validation fails but id_token is valid, callback sets session."""
    from app.auth.token_validation import AuthContext, TokenValidationError

    app = _make_app()
    transport = httpx.ASGITransport(app=app)

    fake_access_token = "fake.access.token"
    id_token = _make_workos_id_token()
    fake_token_response = {
        "access_token": fake_access_token,
        "id_token": id_token,
    }

    with patch("app.web.connections.settings") as mock_settings:
        mock_settings.workos_client_id = _WORKOS_CLIENT_ID
        mock_settings.workos_client_secret = "secret"
        mock_settings.workos_issuer = _WORKOS_ISSUER
        mock_settings.workos_jwks_uri = "https://api.workos.test/sso/jwks/client"
        mock_settings.workos_audience = "https://scheduler.test"
        mock_settings.connections_base_url = "http://localhost:8000"
        mock_settings.web_session_secret = "dev-secret-32-bytes-long-exactly!"

        # First validate_token (access_token) raises; second (id_token) succeeds
        call_count = 0

        def _fake_validate_token(token, *, issuer, audience, jwks_uri, _jwks_client=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise TokenValidationError("access_token not a sub-bearing token")
            return AuthContext(user_id=_WORKOS_SUB)

        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", follow_redirects=False
        ) as client:
            with (
                patch(
                    "app.web.connections.validate_token",
                    side_effect=_fake_validate_token,
                ),
                patch(
                    "httpx.AsyncClient.post",
                    new=AsyncMock(
                        return_value=MagicMock(
                            is_success=True, json=MagicMock(return_value=fake_token_response)
                        )
                    ),
                ),
            ):
                resp = await client.get(
                    "/connections/auth/callback",
                    params={"code": "auth-code-123", "state": ""},
                )

    assert resp.status_code == 302
    assert resp.headers["location"] == "/connections"
    assert "session" in resp.cookies


@pytest.mark.asyncio
async def test_workos_callback_tampered_id_token_returns_502():
    """When access_token fails and id_token has wrong signature, callback returns 502."""
    from app.auth.token_validation import TokenValidationError

    app = _make_app()
    transport = httpx.ASGITransport(app=app)

    other_private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
        backend=default_backend(),
    )
    tampered_id_token = _make_workos_id_token(private_key=other_private_key)
    fake_token_response = {
        "access_token": "fake.access.token",
        "id_token": tampered_id_token,
    }

    with patch("app.web.connections.settings") as mock_settings:
        mock_settings.workos_client_id = _WORKOS_CLIENT_ID
        mock_settings.workos_client_secret = "secret"
        mock_settings.workos_issuer = _WORKOS_ISSUER
        mock_settings.workos_jwks_uri = "https://api.workos.test/sso/jwks/client"
        mock_settings.workos_audience = "https://scheduler.test"
        mock_settings.connections_base_url = "http://localhost:8000"
        mock_settings.web_session_secret = "dev-secret-32-bytes-long-exactly!"

        # Both validate_token calls raise TokenValidationError (wrong key)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", follow_redirects=False
        ) as client:
            with (
                patch(
                    "app.web.connections.validate_token",
                    side_effect=TokenValidationError("bad token"),
                ),
                patch(
                    "httpx.AsyncClient.post",
                    new=AsyncMock(
                        return_value=MagicMock(
                            is_success=True, json=MagicMock(return_value=fake_token_response)
                        )
                    ),
                ),
            ):
                resp = await client.get(
                    "/connections/auth/callback",
                    params={"code": "auth-code-123", "state": ""},
                )

    assert resp.status_code == 502
    assert "session" not in resp.cookies


# ---------------------------------------------------------------------------
# Issue #203 — Migrate from /sso/* to /user_management/*
#
# Background: The SSO flow issues `prof_*` subs (Profile entities) while the
# MCP bearer flow (CIMD/DCR) issues `user_*` subs (User entities). Writing
# under one schema and reading under the other causes every OAuth-gated MCP
# call to return MISSING_CONNECTION despite the /connections UI showing
# "Connected".  These tests pin the AuthKit User Management contract so the
# divergence cannot reappear silently.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connections_login_uses_user_management_authorize_endpoint():
    """`/connections/login` MUST redirect to /user_management/authorize (not /sso/authorize)."""
    app = _make_app()
    transport = httpx.ASGITransport(app=app)
    with patch("app.web.connections.settings") as mock_settings:
        mock_settings.workos_client_id = _WORKOS_CLIENT_ID
        mock_settings.workos_client_secret = "secret"
        mock_settings.workos_issuer = _WORKOS_ISSUER
        mock_settings.workos_api_base_url = "https://api.workos.test"
        mock_settings.connections_base_url = "http://localhost:8000"
        mock_settings.web_session_secret = "dev-secret-32-bytes-long-exactly!"
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", follow_redirects=False
        ) as client:
            resp = await client.get("/connections/login")

    assert resp.status_code == 302
    location = resp.headers["location"]
    assert "/user_management/authorize" in location, (
        f"login must use /user_management/authorize, got: {location}"
    )
    assert "/sso/authorize" not in location, (
        f"login must not use /sso/authorize anymore, got: {location}"
    )


@pytest.mark.asyncio
async def test_user_management_authorize_url_host_is_workos_api_base_not_issuer():
    """Regression net for the 404 caught during the #203 sandbox probe.

    The authorize URL host MUST come from ``settings.workos_api_base_url``
    (api.workos.com), not from ``settings.workos_issuer`` (the AuthKit
    subdomain, which 404s on /user_management/*).
    """
    app = _make_app()
    transport = httpx.ASGITransport(app=app)
    issuer = "https://tenant-abc.authkit.test"
    api_base = "https://api.workos.test"
    with patch("app.web.connections.settings") as mock_settings:
        mock_settings.workos_client_id = _WORKOS_CLIENT_ID
        mock_settings.workos_client_secret = "secret"
        mock_settings.workos_issuer = issuer
        mock_settings.workos_api_base_url = api_base
        mock_settings.connections_base_url = "http://localhost:8000"
        mock_settings.web_session_secret = "dev-secret-32-bytes-long-exactly!"
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", follow_redirects=False
        ) as client:
            resp = await client.get("/connections/login")

    location = resp.headers["location"]
    assert location.startswith(api_base + "/user_management/authorize"), (
        f"authorize URL must be hosted on workos_api_base_url ({api_base}), got: {location}"
    )
    assert not location.startswith(issuer), (
        f"authorize URL must NOT be hosted on workos_issuer ({issuer}) — "
        f"AuthKit subdomain does not serve /user_management/*. Got: {location}"
    )


@pytest.mark.asyncio
async def test_connections_login_uses_provider_authkit():
    """`/connections/login` passes provider=authkit (WorkOS official recommendation).

    Background: with /sso/authorize we had to use provider=GitHubOAuth to route the
    code-exchange to /sso/token; under User Management, provider=authkit lets the
    AuthKit hosted UI handle the chooser.
    """
    app = _make_app()
    transport = httpx.ASGITransport(app=app)
    with patch("app.web.connections.settings") as mock_settings:
        mock_settings.workos_client_id = _WORKOS_CLIENT_ID
        mock_settings.workos_client_secret = "secret"
        mock_settings.workos_issuer = _WORKOS_ISSUER
        mock_settings.workos_api_base_url = "https://api.workos.test"
        mock_settings.connections_base_url = "http://localhost:8000"
        mock_settings.web_session_secret = "dev-secret-32-bytes-long-exactly!"
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", follow_redirects=False
        ) as client:
            resp = await client.get("/connections/login")

    location = resp.headers["location"]
    assert "provider=authkit" in location, f"login must pass provider=authkit, got: {location}"
    assert "provider=GitHubOAuth" not in location, (
        f"GitHubOAuth provider hint must be removed, got: {location}"
    )


@pytest.mark.asyncio
async def test_workos_callback_posts_to_user_management_authenticate_endpoint():
    """WorkOS code exchange MUST POST to api.workos.com/user_management/authenticate.

    Pins both the path (User Management migration, #203) and the host
    (api.workos.com, NOT the AuthKit subdomain — see ADR-063).
    """
    from app.auth.token_validation import AuthContext

    app = _make_app()
    transport = httpx.ASGITransport(app=app)

    captured_url: list[str] = []

    async def _capture_post(self, url, *args, **kwargs):  # noqa: ARG001
        captured_url.append(url)
        return MagicMock(
            is_success=True,
            json=MagicMock(
                return_value={
                    "access_token": "fake.access.token",
                    "user": {"id": "user_01TEST_FROM_AUTHKIT"},
                }
            ),
        )

    api_base = "https://api.workos.test"
    issuer = "https://tenant-abc.authkit.test"
    with patch("app.web.connections.settings") as mock_settings:
        mock_settings.workos_client_id = _WORKOS_CLIENT_ID
        mock_settings.workos_client_secret = "secret"
        mock_settings.workos_issuer = issuer
        mock_settings.workos_api_base_url = api_base
        mock_settings.workos_jwks_uri = f"{issuer}/oauth2/jwks/client"
        mock_settings.workos_audience = _WORKOS_CLIENT_ID
        mock_settings.connections_base_url = "http://localhost:8000"
        mock_settings.web_session_secret = "dev-secret-32-bytes-long-exactly!"

        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", follow_redirects=False
        ) as client:
            with (
                patch(
                    "app.web.connections.validate_token",
                    return_value=AuthContext(user_id="user_01TEST_FROM_AUTHKIT"),
                ),
                patch("httpx.AsyncClient.post", new=_capture_post),
            ):
                resp = await client.get(
                    "/connections/auth/callback",
                    params={"code": "auth-code-123", "state": ""},
                )

    assert resp.status_code == 302
    assert len(captured_url) == 1, "callback should POST exactly once for code exchange"
    expected_url = f"{api_base}/user_management/authenticate"
    assert captured_url[0] == expected_url, (
        f"code exchange must POST to {expected_url}, got: {captured_url[0]}"
    )
    assert not captured_url[0].startswith(issuer), (
        f"code exchange must NOT post to the AuthKit subdomain ({issuer}), got: {captured_url[0]}"
    )


@pytest.mark.asyncio
async def test_workos_callback_extracts_user_id_from_user_object():
    """When access_token+id_token both fail, callback MUST fall back to response.user.id.

    User Management returns `{user: {id: "user_..."}}` (not `{profile: {id: "prof_..."}}`).
    This is the failure mode that caused issue #203 (rows written under prof_*).
    """
    from app.auth.token_validation import TokenValidationError

    app = _make_app()
    transport = httpx.ASGITransport(app=app)

    expected_user_id = "user_01TEST_FROM_AUTHKIT_USER_OBJECT"
    fake_token_response = {
        "access_token": "opaque_token_no_jwt",
        "user": {"id": expected_user_id, "email": "test@example.com"},
    }

    with patch("app.web.connections.settings") as mock_settings:
        mock_settings.workos_client_id = _WORKOS_CLIENT_ID
        mock_settings.workos_client_secret = "secret"
        mock_settings.workos_issuer = _WORKOS_ISSUER
        mock_settings.workos_api_base_url = "https://api.workos.test"
        mock_settings.workos_jwks_uri = "https://api.workos.test/oauth2/jwks/client"
        mock_settings.workos_audience = _WORKOS_CLIENT_ID
        mock_settings.connections_base_url = "http://localhost:8000"
        mock_settings.web_session_secret = "dev-secret-32-bytes-long-exactly!"

        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", follow_redirects=False
        ) as client:
            with (
                patch(
                    "app.web.connections.validate_token",
                    side_effect=TokenValidationError("opaque token has no JWT claims"),
                ),
                patch(
                    "httpx.AsyncClient.post",
                    new=AsyncMock(
                        return_value=MagicMock(
                            is_success=True,
                            json=MagicMock(return_value=fake_token_response),
                        )
                    ),
                ),
            ):
                resp = await client.get(
                    "/connections/auth/callback",
                    params={"code": "auth-code-123", "state": ""},
                )

    assert resp.status_code == 302
    assert resp.headers["location"] == "/connections"

    # Decode the session cookie and assert the sub is user_01..., NOT prof_01...
    session_cookie = resp.cookies.get("session")
    assert session_cookie is not None, "callback must set a session cookie"
    decoded = jwt.decode(session_cookie, "dev-secret-32-bytes-long-exactly!", algorithms=["HS256"])
    assert decoded["sub"] == expected_user_id, (
        f"session sub must equal user.id from User Management response, got: {decoded['sub']}"
    )
    assert decoded["sub"].startswith("user_"), (
        f"session sub must be a User Management ID (user_*), got: {decoded['sub']}"
    )
