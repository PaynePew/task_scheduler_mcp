"""Unit tests for app/auth/token_validation.py (ADR-053).

Strategy:
  - Generate an RSA key pair at module scope (cheap; reused across all tests).
  - Sign fixture JWTs with the private key.
  - Supply a mock PyJWKClient whose get_signing_key_from_jwt returns the
    corresponding public key, bypassing real JWKS network calls.
  - Assert valid token → AuthContext{user_id == sub}.
  - Assert expired / wrong-audience / bad-signature → TokenValidationError.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock

import jwt
import pytest
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.asymmetric import rsa

from app.auth.token_validation import AuthContext, TokenValidationError, validate_token

# ---------------------------------------------------------------------------
# RSA key pair shared across all tests in this module
# ---------------------------------------------------------------------------

_PRIVATE_KEY = rsa.generate_private_key(
    public_exponent=65537,
    key_size=2048,
    backend=default_backend(),
)
_PUBLIC_KEY = _PRIVATE_KEY.public_key()

_ISSUER = "https://api.workos.test"
_AUDIENCE = "https://scheduler.test"
_SUB = "user_01HXYZ"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_jwks_client(key: Any = _PUBLIC_KEY) -> MagicMock:
    """Return a mock PyJWKClient whose get_signing_key_from_jwt yields *key*."""
    signing_key = MagicMock()
    signing_key.key = key
    client = MagicMock()
    client.get_signing_key_from_jwt.return_value = signing_key
    return client


def _make_token(
    *,
    sub: str = _SUB,
    aud: str | list[str] = _AUDIENCE,
    iss: str = _ISSUER,
    exp_offset: int = 3600,
    private_key: Any = _PRIVATE_KEY,
    algorithm: str = "RS256",
    extra_claims: dict | None = None,
) -> str:
    now = int(time.time())
    payload: dict = {
        "sub": sub,
        "aud": aud,
        "iss": iss,
        "iat": now,
        "exp": now + exp_offset,
    }
    if extra_claims:
        payload.update(extra_claims)
    return jwt.encode(payload, private_key, algorithm=algorithm)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_valid_token_returns_auth_context():
    token = _make_token()
    ctx = validate_token(
        token,
        issuer=_ISSUER,
        audience=_AUDIENCE,
        jwks_uri="https://unused.test/jwks",
        _jwks_client=_make_mock_jwks_client(),
    )
    assert isinstance(ctx, AuthContext)
    assert ctx.user_id == _SUB


def test_expired_token_raises():
    token = _make_token(exp_offset=-1)  # already expired
    with pytest.raises(TokenValidationError, match="expired"):
        validate_token(
            token,
            issuer=_ISSUER,
            audience=_AUDIENCE,
            jwks_uri="https://unused.test/jwks",
            _jwks_client=_make_mock_jwks_client(),
        )


def test_wrong_audience_raises():
    token = _make_token(aud="https://other-service.test")
    with pytest.raises(TokenValidationError):
        validate_token(
            token,
            issuer=_ISSUER,
            audience=_AUDIENCE,
            jwks_uri="https://unused.test/jwks",
            _jwks_client=_make_mock_jwks_client(),
        )


def test_bad_signature_raises():
    other_private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
        backend=default_backend(),
    )
    token = _make_token(private_key=other_private_key)
    # mock client returns the ORIGINAL public key → signature mismatch
    with pytest.raises(TokenValidationError):
        validate_token(
            token,
            issuer=_ISSUER,
            audience=_AUDIENCE,
            jwks_uri="https://unused.test/jwks",
            _jwks_client=_make_mock_jwks_client(key=_PUBLIC_KEY),
        )


def test_missing_sub_raises():
    now = int(time.time())
    payload = {
        "aud": _AUDIENCE,
        "iss": _ISSUER,
        "iat": now,
        "exp": now + 3600,
        # no "sub"
    }
    # jwt.encode without sub; decode will fail on "require" list
    token = jwt.encode(payload, _PRIVATE_KEY, algorithm="RS256")
    with pytest.raises(TokenValidationError):
        validate_token(
            token,
            issuer=_ISSUER,
            audience=_AUDIENCE,
            jwks_uri="https://unused.test/jwks",
            _jwks_client=_make_mock_jwks_client(),
        )


def test_wrong_issuer_raises():
    token = _make_token(iss="https://evil.example.com")
    with pytest.raises(TokenValidationError):
        validate_token(
            token,
            issuer=_ISSUER,
            audience=_AUDIENCE,
            jwks_uri="https://unused.test/jwks",
            _jwks_client=_make_mock_jwks_client(),
        )
