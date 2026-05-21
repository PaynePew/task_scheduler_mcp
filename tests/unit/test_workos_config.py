"""Unit tests for WorkOS partial-config fail-closed behavior (issue #159).

Covers all four cases:
  0 vars set  → trust-only (no error, INFO log emitted at module level)
  1 var set   → ValidationError at startup naming the missing vars
  2 vars set  → ValidationError at startup naming the missing var
  3 vars set  → auth-enabled (no error)
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError


def _make_settings(**kw):
    """Return a fresh Settings instance with only the given env vars set."""
    from app.config.settings import Settings

    # Use _env_file=None so no .env file on disk bleeds in during tests.
    return Settings(
        _env_file=None,
        **kw,
    )


# ---------------------------------------------------------------------------
# AC: zero WorkOS vars → trust-only, no error
# ---------------------------------------------------------------------------


def test_zero_workos_vars_allowed():
    """All three unset → Settings instantiation succeeds."""
    s = _make_settings()
    assert s.workos_jwks_uri is None
    assert s.workos_issuer is None
    assert s.workos_audience is None


# ---------------------------------------------------------------------------
# AC: all three set → auth-enabled, no error
# ---------------------------------------------------------------------------


def test_all_three_workos_vars_allowed():
    """All three set → Settings instantiation succeeds."""
    s = _make_settings(
        workos_jwks_uri="https://api.workos.com/sso/jwks/client_123",
        workos_issuer="https://api.workos.com",
        workos_audience="https://scheduler.example.com",
    )
    assert s.workos_jwks_uri == "https://api.workos.com/sso/jwks/client_123"
    assert s.workos_issuer == "https://api.workos.com"
    assert s.workos_audience == "https://scheduler.example.com"


# ---------------------------------------------------------------------------
# AC: exactly 1 set → fail fast with clear error naming the missing vars
# ---------------------------------------------------------------------------


def test_one_workos_var_raises():
    """One var set → ValidationError with message naming the missing vars."""
    with pytest.raises(ValidationError) as exc_info:
        _make_settings(workos_jwks_uri="https://api.workos.com/sso/jwks/client_123")
    msg = str(exc_info.value)
    assert "WORKOS_ISSUER" in msg
    assert "WORKOS_AUDIENCE" in msg


def test_one_workos_var_raises_issuer_only():
    """Only WORKOS_ISSUER set → ValidationError."""
    with pytest.raises(ValidationError) as exc_info:
        _make_settings(workos_issuer="https://api.workos.com")
    msg = str(exc_info.value)
    assert "WORKOS_JWKS_URI" in msg
    assert "WORKOS_AUDIENCE" in msg


def test_one_workos_var_raises_audience_only():
    """Only WORKOS_AUDIENCE set → ValidationError."""
    with pytest.raises(ValidationError) as exc_info:
        _make_settings(workos_audience="https://scheduler.example.com")
    msg = str(exc_info.value)
    assert "WORKOS_JWKS_URI" in msg
    assert "WORKOS_ISSUER" in msg


# ---------------------------------------------------------------------------
# AC: exactly 2 set → fail fast with clear error naming the missing var
# ---------------------------------------------------------------------------


def test_two_workos_vars_raises_missing_audience():
    """jwks_uri + issuer set, audience missing → ValidationError naming WORKOS_AUDIENCE."""
    with pytest.raises(ValidationError) as exc_info:
        _make_settings(
            workos_jwks_uri="https://api.workos.com/sso/jwks/client_123",
            workos_issuer="https://api.workos.com",
        )
    msg = str(exc_info.value)
    assert "WORKOS_AUDIENCE" in msg


def test_two_workos_vars_raises_missing_issuer():
    """jwks_uri + audience set, issuer missing → ValidationError naming WORKOS_ISSUER."""
    with pytest.raises(ValidationError) as exc_info:
        _make_settings(
            workos_jwks_uri="https://api.workos.com/sso/jwks/client_123",
            workos_audience="https://scheduler.example.com",
        )
    msg = str(exc_info.value)
    assert "WORKOS_ISSUER" in msg


def test_two_workos_vars_raises_missing_jwks_uri():
    """issuer + audience set, jwks_uri missing → ValidationError naming WORKOS_JWKS_URI."""
    with pytest.raises(ValidationError) as exc_info:
        _make_settings(
            workos_issuer="https://api.workos.com",
            workos_audience="https://scheduler.example.com",
        )
    msg = str(exc_info.value)
    assert "WORKOS_JWKS_URI" in msg
