"""Unit tests for mcp_http auth branch dispatch (issue #175).

Injects TrustOnly() and BearerVerified(...) directly via monkeypatch to verify
each branch's behaviour without needing real WorkOS credentials or network calls.
"""

from __future__ import annotations

from starlette.requests import Request

from app.auth.posture import BearerVerified, TrustOnly
from app.auth.token_validation import AuthContext, TokenValidationError
from app.entrypoints import mcp_http
from app.entrypoints.mcp_http import _resolve_user_id

_BEARER_POSTURE = BearerVerified(
    jwks_uri="https://jwks.example.com",
    issuer="https://iss.example.com",
    audience="https://aud.example.com",
)


def _make_request(headers: dict[str, str]) -> Request:
    """Build a minimal Starlette Request for testing _resolve_user_id."""
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "query_string": b"",
    }
    return Request(scope)


# ---------------------------------------------------------------------------
# TrustOnly branch
# ---------------------------------------------------------------------------


def test_trust_only_x_user_id_header_returned(monkeypatch):
    """TrustOnly: X-User-Id header present → that user_id is returned."""
    monkeypatch.setattr(mcp_http, "POSTURE", TrustOnly())
    request = _make_request({"x-user-id": "alice"})
    user_id, err = _resolve_user_id(request)
    assert user_id == "alice"
    assert err is None


def test_trust_only_fallback_without_header(monkeypatch):
    """TrustOnly: no X-User-Id header → resolve_user_id_stdio fallback used."""
    monkeypatch.setattr(mcp_http, "POSTURE", TrustOnly())
    monkeypatch.setattr(mcp_http, "resolve_user_id_stdio", lambda: "stdio-user")
    request = _make_request({})
    user_id, err = _resolve_user_id(request)
    assert user_id == "stdio-user"
    assert err is None


def test_trust_only_bearer_header_ignored(monkeypatch):
    """TrustOnly: Authorization header is ignored; X-User-Id takes precedence."""
    monkeypatch.setattr(mcp_http, "POSTURE", TrustOnly())
    request = _make_request({"x-user-id": "bob", "authorization": "Bearer some-token"})
    user_id, err = _resolve_user_id(request)
    assert user_id == "bob"
    assert err is None


# ---------------------------------------------------------------------------
# BearerVerified branch
# ---------------------------------------------------------------------------


def test_bearer_verified_no_token_returns_401(monkeypatch):
    """BearerVerified: no Authorization header → (None, 401 response)."""
    monkeypatch.setattr(mcp_http, "POSTURE", _BEARER_POSTURE)
    request = _make_request({})
    user_id, err = _resolve_user_id(request)
    assert user_id is None
    assert err is not None
    assert err.status_code == 401
    assert "WWW-Authenticate" in err.headers


def test_bearer_verified_x_user_id_not_accepted(monkeypatch):
    """BearerVerified: X-User-Id without Bearer token → 401 (header ignored)."""
    monkeypatch.setattr(mcp_http, "POSTURE", _BEARER_POSTURE)
    request = _make_request({"x-user-id": "alice"})
    user_id, err = _resolve_user_id(request)
    assert user_id is None
    assert err is not None
    assert err.status_code == 401


def test_bearer_verified_invalid_token_returns_401(monkeypatch):
    """BearerVerified: invalid Bearer token → (None, 401 response)."""
    monkeypatch.setattr(mcp_http, "POSTURE", _BEARER_POSTURE)

    def _fail_validate(token, *, issuer, audience, jwks_uri, _jwks_client=None):
        raise TokenValidationError("invalid token")

    monkeypatch.setattr(mcp_http, "validate_token", _fail_validate)
    request = _make_request({"authorization": "Bearer invalid-token"})
    user_id, err = _resolve_user_id(request)
    assert user_id is None
    assert err is not None
    assert err.status_code == 401
    assert "WWW-Authenticate" in err.headers


def test_bearer_verified_valid_token_returns_user_id(monkeypatch):
    """BearerVerified: valid Bearer token → (verified user_id, None)."""
    monkeypatch.setattr(mcp_http, "POSTURE", _BEARER_POSTURE)

    def _ok_validate(token, *, issuer, audience, jwks_uri, _jwks_client=None):
        return AuthContext(user_id="verified-user")

    monkeypatch.setattr(mcp_http, "validate_token", _ok_validate)
    request = _make_request({"authorization": "Bearer valid-token"})
    user_id, err = _resolve_user_id(request)
    assert user_id == "verified-user"
    assert err is None


def test_bearer_verified_passes_destructured_kwargs_to_validate(monkeypatch):
    """BearerVerified: validate_token receives the posture's jwks_uri/issuer/audience."""
    posture = BearerVerified(
        jwks_uri="https://jwks.custom.example.com",
        issuer="https://iss.custom.example.com",
        audience="https://aud.custom.example.com",
    )
    monkeypatch.setattr(mcp_http, "POSTURE", posture)

    captured: dict[str, str] = {}

    def _capture(token, *, issuer, audience, jwks_uri, _jwks_client=None):
        captured["issuer"] = issuer
        captured["audience"] = audience
        captured["jwks_uri"] = jwks_uri
        return AuthContext(user_id="test-user")

    monkeypatch.setattr(mcp_http, "validate_token", _capture)
    request = _make_request({"authorization": "Bearer some-token"})
    _resolve_user_id(request)
    assert captured["jwks_uri"] == "https://jwks.custom.example.com"
    assert captured["issuer"] == "https://iss.custom.example.com"
    assert captured["audience"] == "https://aud.custom.example.com"
