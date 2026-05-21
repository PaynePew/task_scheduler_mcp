"""Integration smoke tests for the /connections web surface (ADR-058).

AC7: /connections requires auth; an unauthenticated visit redirects to login.
     A fake OAuth callback stores a connection.

These tests run against real Postgres (via the harness-injected DATABASE_URL)
with moto for KMS, so no real AWS or GitHub credentials are needed.
"""

from __future__ import annotations

import boto3
import httpx
import pytest
import pytest_asyncio
from moto import mock_aws
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.engine import create_async_engine
from app.db.models import OAuthConnection
from app.entrypoints.mcp_http import build_app
from app.web.connections import _create_session_token

_REGION = "ap-northeast-1"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def session_factory():
    """Fresh engine per test; removes oauth_connections rows on teardown."""
    engine = create_async_engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    async with factory() as session:
        async with session.begin():
            from sqlalchemy import delete

            await session.execute(delete(OAuthConnection))
    await engine.dispose()


@pytest.fixture
def mcp_app(session_factory):
    """In-process Starlette app with a per-test session factory."""
    return build_app(json_response=True, session_factory=session_factory, shed_fn=lambda: False)


# ---------------------------------------------------------------------------
# Smoke tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_connections_unauthenticated_redirects_to_login(mcp_app):
    """AC7 part 1: unauthenticated visit to /connections redirects to /connections/login."""
    transport = httpx.ASGITransport(app=mcp_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as client:
        resp = await client.get("/connections")

    assert resp.status_code == 302
    assert resp.headers["location"] == "/connections/login"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_connections_authenticated_returns_dashboard(mcp_app):
    """Authenticated user sees the connections dashboard page."""
    token = _create_session_token("test-user-integration")
    transport = httpx.ASGITransport(app=mcp_app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={"session": token},
        follow_redirects=False,
    ) as client:
        from unittest.mock import patch

        with patch("app.web.connections._make_kms_envelope", return_value=None):
            resp = await client.get("/connections")

    assert resp.status_code == 200
    assert "GitHub" in resp.text
    assert "test-user-integration" in resp.text


@pytest.mark.integration
@pytest.mark.asyncio
async def test_fake_oauth_callback_stores_connection(mcp_app, session_factory):
    """AC7 part 2: a fake OAuth callback stores a connection in the DB.

    We bypass the real GitHub token exchange by patching _exchange_github_code
    and _make_kms_envelope, then verify the connection was persisted to Postgres.
    """
    with mock_aws():
        # Set up a fake KMS key
        kms_client = boto3.client("kms", region_name=_REGION)
        key_id = kms_client.create_key(Description="test")["KeyMetadata"]["KeyId"]

        from app.crypto.kms_envelope import KmsEnvelope

        fake_envelope = KmsEnvelope(kms_client=kms_client, key_id=key_id)
        fake_token_data = {"access_token": "ghs_fake_integration_token", "scope": "repo"}

        token = _create_session_token("test-user-integration")
        transport = httpx.ASGITransport(app=mcp_app)

        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            cookies={"session": token},
            follow_redirects=False,
        ) as client:
            from unittest.mock import AsyncMock, patch

            with (
                patch("app.web.connections._make_kms_envelope", return_value=fake_envelope),
                patch(
                    "app.web.connections._exchange_github_code",
                    new=AsyncMock(return_value=fake_token_data),
                ),
            ):
                resp = await client.get(
                    "/connections/github/callback",
                    params={"code": "fake-code", "state": ""},
                )

        assert resp.status_code == 302
        assert resp.headers["location"] == "/connections"

        # Verify connection was stored in the real Postgres DB
        from sqlalchemy import select

        async with session_factory() as session:
            result = await session.execute(
                select(OAuthConnection).where(
                    OAuthConnection.user_id == "test-user-integration",
                    OAuthConnection.provider == "github",
                )
            )
            row = result.scalar_one_or_none()

        assert row is not None, "Connection was not persisted to Postgres"
        # Verify it's actually encrypted (no plaintext token in the blob)
        assert b"ghs_fake_integration_token" not in row.encrypted_blob


@pytest.mark.integration
@pytest.mark.asyncio
async def test_login_trust_only_sets_session(mcp_app):
    """In trust-only mode, /connections/login sets a session cookie."""
    transport = httpx.ASGITransport(app=mcp_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as client:
        resp = await client.get("/connections/login")

    assert resp.status_code == 302
    assert resp.headers["location"] == "/connections"
    assert "session" in resp.cookies
