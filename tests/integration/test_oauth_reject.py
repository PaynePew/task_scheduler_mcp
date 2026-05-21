"""Integration smoke test: unauthenticated HTTP /mcp is rejected (ADR-053).

The app is started with WorkOS auth enabled by patching the module-level
POSTURE to a BearerVerified instance so the test doesn't need real WorkOS
credentials while still exercising the 401-response code path.

Run with:
    uv run pytest -m integration tests/integration/test_oauth_reject.py
"""

from __future__ import annotations

import httpx
import pytest

from app.auth.posture import BearerVerified
from app.entrypoints import mcp_http

MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
}


@pytest.fixture
def auth_enabled_app(monkeypatch):
    """Build the MCP HTTP ASGI app with POSTURE forced to BearerVerified.

    This simulates a deployment with WorkOS credentials configured without
    needing real credentials — the middleware gate is exercised, requests
    are rejected before any WorkOS call is made.
    """
    monkeypatch.setattr(
        mcp_http,
        "POSTURE",
        BearerVerified(
            jwks_uri="https://api.workos.com/sso/jwks/client_test",
            issuer="https://api.workos.com",
            audience="https://scheduler.example.com",
        ),
    )
    # Disable health-based load shedding: should_shed() reads live CPU via
    # psutil, so on a saturated CI runner (cpu>=90%) the /mcp request is
    # shed with 503 *before* auth runs — masking the 401 these tests assert.
    app = mcp_http.build_app(json_response=True, shed_fn=lambda: False)
    return app


@pytest.mark.integration
async def test_unauthenticated_mcp_returns_401(auth_enabled_app):
    """No Authorization header → 401 with WWW-Authenticate."""
    transport = httpx.ASGITransport(app=auth_enabled_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            headers=MCP_HEADERS,
        )
    assert resp.status_code == 401
    assert "WWW-Authenticate" in resp.headers


@pytest.mark.integration
async def test_invalid_bearer_returns_401(auth_enabled_app):
    """Malformed / non-JWT Bearer token → 401 with WWW-Authenticate."""
    transport = httpx.ASGITransport(app=auth_enabled_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            headers={**MCP_HEADERS, "Authorization": "Bearer not-a-valid-jwt"},
        )
    assert resp.status_code == 401
    assert "WWW-Authenticate" in resp.headers


@pytest.mark.integration
async def test_x_user_id_header_ignored(auth_enabled_app):
    """X-User-Id header must not bypass auth when auth is enabled."""
    transport = httpx.ASGITransport(app=auth_enabled_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}},
            headers={**MCP_HEADERS, "X-User-Id": "alice"},
        )
    assert resp.status_code == 401


@pytest.mark.integration
async def test_prm_endpoint_reachable(auth_enabled_app):
    """Protected Resource Metadata endpoint responds at the RFC 9728 path."""
    transport = httpx.ASGITransport(app=auth_enabled_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/.well-known/oauth-protected-resource")
    assert resp.status_code == 200
    body = resp.json()
    assert "resource" in body
    assert "bearer_methods_supported" in body
