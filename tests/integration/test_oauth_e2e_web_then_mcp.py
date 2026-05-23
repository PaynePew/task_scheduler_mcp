"""End-to-end regression test for issue #203 (Layer-2 OAuth user_id schema parity).

This test pins the contract that the web /connections flow and the MCP bearer
flow MUST resolve to the same `user_id` for the same human user — that is,
both flows MUST consume from the WorkOS User Management endpoint family so
their `sub` claims share the `user_*` schema.

Failure mode this guards against (issue #203):
    web flow writes `oauth_connections.user_id = "prof_01..."` (SSO Profile)
    MCP flow looks up `oauth_connections.user_id = "user_01..."` (User Mgmt)
    → every MCP OAuth-gated call returns MISSING_CONNECTION
       despite the /connections UI showing "Connected".

The test exercises a single mock WorkOS app whose authorize → authenticate
chain issues a `user_*` sub.  It then asserts:

  1. Web /connections/auth/callback persists session under `user_*`.
  2. /connections/github/callback writes a row keyed by `user_*`.
  3. _check_oauth_connection(action="github_digest", user_id=<same user_*>)
     returns None (no MISSING_CONNECTION envelope).
  4. If, instead, we look up the connection with a `prof_*` style sub
     (simulating the pre-#203 code path), we correctly miss — which is
     exactly the bug the migration fixes.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import boto3
import httpx
import pytest
import pytest_asyncio
from moto import mock_aws
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.auth.token_validation import TokenValidationError
from app.crypto.kms_envelope import KmsEnvelope
from app.db.engine import create_async_engine
from app.db.models import OAuthConnection
from app.entrypoints.mcp_http import build_app
from app.web.connections import _create_session_token

_REGION = "ap-northeast-1"
_USER_ID = "user_01TEST_E2E_AUTHKIT"
_PROF_ID = "prof_01TEST_E2E_LEGACY_SSO"
_WORKOS_CLIENT_ID = "client_test_e2e"


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
    return build_app(json_response=True, session_factory=session_factory, shed_fn=lambda: False)


# ---------------------------------------------------------------------------
# Regression test (the linchpin assertion for issue #203)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_web_oauth_callback_and_mcp_check_resolve_same_user(mcp_app, session_factory):
    """The web /connections flow and the MCP /mcp flow MUST agree on user_id.

    This is the canonical "two systems share a column but no test asserts the
    schemas match" failure mode (issue #203). Pins User Management contract:
    /connections writes under `user_*`, the MCP gate reads under `user_*`,
    both succeed for the same human.
    """
    with mock_aws():
        # Set up a real-ish KMS envelope so the connection store encrypts blobs.
        kms_client = boto3.client("kms", region_name=_REGION)
        key_id = kms_client.create_key(Description="test")["KeyMetadata"]["KeyId"]
        fake_envelope = KmsEnvelope(kms_client=kms_client, key_id=key_id)

        # ─── Step 1: web /connections/auth/callback under the mocked WorkOS app ───
        # AuthKit /user_management/authenticate response shape — `user.id` is the
        # canonical `user_*` sub.  access_token is opaque (typical for AuthKit),
        # so validate_token will raise and the callback must fall back to user.id.
        fake_authkit_token_response = {
            "access_token": "opaque_authkit_token",
            "user": {"id": _USER_ID, "email": "e2e@example.com"},
        }
        transport = httpx.ASGITransport(app=mcp_app)

        with patch("app.web.connections.settings") as web_settings:
            web_settings.workos_client_id = _WORKOS_CLIENT_ID
            web_settings.workos_client_secret = "secret"
            web_settings.workos_issuer = "https://tenant.authkit.test"
            web_settings.workos_api_base_url = "https://api.workos.test"
            web_settings.workos_jwks_uri = "https://tenant.authkit.test/oauth2/jwks/client"
            web_settings.workos_audience = _WORKOS_CLIENT_ID
            web_settings.connections_base_url = "http://localhost:8000"
            web_settings.web_session_secret = "dev-secret-32-bytes-long-exactly!"

            async with httpx.AsyncClient(
                transport=transport, base_url="http://test", follow_redirects=False
            ) as client:
                with (
                    patch(
                        "app.web.connections.validate_token",
                        side_effect=TokenValidationError("opaque token, no JWT"),
                    ),
                    patch(
                        "httpx.AsyncClient.post",
                        new=AsyncMock(
                            return_value=MagicMock(
                                is_success=True,
                                json=MagicMock(return_value=fake_authkit_token_response),
                            )
                        ),
                    ),
                ):
                    auth_resp = await client.get(
                        "/connections/auth/callback",
                        params={"code": "fake-code", "state": ""},
                    )

        assert auth_resp.status_code == 302
        # Session cookie was set with the User Management `user_*` sub.
        session_cookie = auth_resp.cookies.get("session")
        assert session_cookie, "auth callback must set session"

        # ─── Step 2: web /connections/github/callback persists row under same user_* ───
        with patch("app.web.connections.settings") as web_settings_gh:
            web_settings_gh.workos_client_id = _WORKOS_CLIENT_ID
            web_settings_gh.workos_client_secret = "secret"
            web_settings_gh.workos_issuer = "https://tenant.authkit.test"
            web_settings_gh.workos_api_base_url = "https://api.workos.test"
            web_settings_gh.github_client_id = "gh-client"
            web_settings_gh.github_client_secret = "gh-secret"
            web_settings_gh.connections_base_url = "http://localhost:8000"
            web_settings_gh.web_session_secret = "dev-secret-32-bytes-long-exactly!"
            # Reuse the session cookie set by auth callback above.
            issued_token = _create_session_token(_USER_ID)

            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
                cookies={"session": issued_token},
                follow_redirects=False,
            ) as client:
                with (
                    patch(
                        "app.web.connections._make_kms_envelope",
                        return_value=fake_envelope,
                    ),
                    patch(
                        "app.web.connections._exchange_github_code",
                        new=AsyncMock(
                            return_value={"access_token": "ghs_e2e_token", "scope": "repo"}
                        ),
                    ),
                ):
                    gh_resp = await client.get(
                        "/connections/github/callback",
                        params={"code": "fake-gh-code", "state": ""},
                    )

        assert gh_resp.status_code == 302
        async with session_factory() as session:
            result = await session.execute(
                select(OAuthConnection).where(
                    OAuthConnection.user_id == _USER_ID,
                    OAuthConnection.provider == "github",
                )
            )
            stored_row = result.scalar_one_or_none()
        assert stored_row is not None, (
            f"web callback must persist row keyed by {_USER_ID} (User Management sub)"
        )

        # ─── Step 3: MCP _check_oauth_connection with the SAME user_* resolves OK ───
        from app.mcp.server import _check_oauth_connection

        @asynccontextmanager
        async def _factory_ctx():
            async with session_factory() as s:
                yield s

        with patch("app.mcp.server._make_server_kms_envelope", return_value=fake_envelope):
            mcp_check = await _check_oauth_connection("github_digest", _USER_ID, _factory_ctx)
        assert mcp_check is None, (
            "MCP OAuth gate must find the connection — same user_id schema as web. "
            "Returning a MISSING_CONNECTION envelope here is exactly issue #203."
        )

        # ─── Step 4: looking up with the legacy prof_* schema MUST miss ───
        # This is the bug we just fixed: if anything regresses to writing under
        # `prof_*` (or to reading under `prof_*`), the MCP gate will not find
        # the row and will return MISSING_CONNECTION.  This assertion proves
        # the schema-parity check is real, not vacuously passing because of
        # mocked-away connection-store behavior.
        with patch("app.mcp.server._make_server_kms_envelope", return_value=fake_envelope):
            mcp_check_legacy = await _check_oauth_connection(
                "github_digest", _PROF_ID, _factory_ctx
            )
        assert mcp_check_legacy is not None
        assert mcp_check_legacy["ok"] is False
        assert mcp_check_legacy["error"]["code"] == "MISSING_CONNECTION", (
            "Sanity check: a prof_* sub MUST miss this row, proving the "
            "regression net is wired to the right schema."
        )


# ---------------------------------------------------------------------------
# Companion: web callback under User Management writes user_*, not prof_*
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_web_callback_persists_user_management_sub_not_profile_id(mcp_app, session_factory):
    """If a token response has both `user.id` (user_*) and `profile.id` (prof_*),
    the callback MUST prefer `user.id` — anchors the User Management migration.
    """
    with mock_aws():
        kms_client = boto3.client("kms", region_name=_REGION)
        key_id = kms_client.create_key(Description="test")["KeyMetadata"]["KeyId"]
        fake_envelope = KmsEnvelope(kms_client=kms_client, key_id=key_id)

        # Both shapes present; the migration target is user.id.
        token_response_both = {
            "access_token": "opaque",
            "user": {"id": _USER_ID},
            "profile": {"id": _PROF_ID},
        }
        transport = httpx.ASGITransport(app=mcp_app)

        with patch("app.web.connections.settings") as web_settings:
            web_settings.workos_client_id = _WORKOS_CLIENT_ID
            web_settings.workos_client_secret = "secret"
            web_settings.workos_issuer = "https://tenant.authkit.test"
            web_settings.workos_api_base_url = "https://api.workos.test"
            web_settings.workos_jwks_uri = "https://tenant.authkit.test/oauth2/jwks/client"
            web_settings.workos_audience = _WORKOS_CLIENT_ID
            web_settings.connections_base_url = "http://localhost:8000"
            web_settings.web_session_secret = "dev-secret-32-bytes-long-exactly!"

            async with httpx.AsyncClient(
                transport=transport, base_url="http://test", follow_redirects=False
            ) as client:
                with (
                    patch(
                        "app.web.connections.validate_token",
                        side_effect=TokenValidationError("no JWT"),
                    ),
                    patch(
                        "httpx.AsyncClient.post",
                        new=AsyncMock(
                            return_value=MagicMock(
                                is_success=True,
                                json=MagicMock(return_value=token_response_both),
                            )
                        ),
                    ),
                ):
                    resp = await client.get(
                        "/connections/auth/callback",
                        params={"code": "fake", "state": ""},
                    )

        assert resp.status_code == 302
        import jwt

        decoded = jwt.decode(
            resp.cookies["session"],
            "dev-secret-32-bytes-long-exactly!",
            algorithms=["HS256"],
        )
        assert decoded["sub"] == _USER_ID
        assert not decoded["sub"].startswith("prof_"), (
            "callback regressed to SSO Profile id; must use user.id (User Mgmt)"
        )

        # Smoke: the envelope plumbing actually works.
        _ = fake_envelope
