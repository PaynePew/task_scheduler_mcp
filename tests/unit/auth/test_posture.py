"""Unit tests for app.auth.posture.bearer_posture_from_settings (issue #172).

Calls the factory directly — no Pydantic/Settings round-trip.
10 detailed cases covering all branches of the sum type.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.auth.posture import BearerVerified, TrustOnly, bearer_posture_from_settings


def _ns(jwks_uri=None, issuer=None, audience=None):
    """Build a minimal settings-like namespace with the three WorkOS fields."""
    return SimpleNamespace(
        workos_jwks_uri=jwks_uri,
        workos_issuer=issuer,
        workos_audience=audience,
    )


_JWKS = "https://api.workos.com/sso/jwks/client_123"
_ISSUER = "https://api.workos.com"
_AUDIENCE = "https://scheduler.example.com"


# ---------------------------------------------------------------------------
# Trust-only cases (all absent / all empty)
# ---------------------------------------------------------------------------


def test_all_none_returns_trust_only():
    """All three None → TrustOnly()."""
    result = bearer_posture_from_settings(_ns())
    assert isinstance(result, TrustOnly)


def test_all_empty_string_returns_trust_only():
    """All three '' → TrustOnly (empty string treated as absent)."""
    result = bearer_posture_from_settings(_ns(jwks_uri="", issuer="", audience=""))
    assert isinstance(result, TrustOnly)


# ---------------------------------------------------------------------------
# Auth-enabled case (all three present)
# ---------------------------------------------------------------------------


def test_all_three_present_returns_bearer_verified():
    """All three set → BearerVerified with the correct field values."""
    result = bearer_posture_from_settings(_ns(jwks_uri=_JWKS, issuer=_ISSUER, audience=_AUDIENCE))
    assert isinstance(result, BearerVerified)
    assert result.jwks_uri == _JWKS
    assert result.issuer == _ISSUER
    assert result.audience == _AUDIENCE


# ---------------------------------------------------------------------------
# Partial config — exactly 1 present → ValueError
# ---------------------------------------------------------------------------


def test_only_jwks_uri_raises():
    """Only jwks_uri set → ValueError naming WORKOS_ISSUER and WORKOS_AUDIENCE."""
    with pytest.raises(ValueError) as exc_info:
        bearer_posture_from_settings(_ns(jwks_uri=_JWKS))
    msg = str(exc_info.value)
    assert "WORKOS_ISSUER" in msg
    assert "WORKOS_AUDIENCE" in msg


def test_only_issuer_raises():
    """Only issuer set → ValueError naming WORKOS_JWKS_URI and WORKOS_AUDIENCE."""
    with pytest.raises(ValueError) as exc_info:
        bearer_posture_from_settings(_ns(issuer=_ISSUER))
    msg = str(exc_info.value)
    assert "WORKOS_JWKS_URI" in msg
    assert "WORKOS_AUDIENCE" in msg


def test_only_audience_raises():
    """Only audience set → ValueError naming WORKOS_JWKS_URI and WORKOS_ISSUER."""
    with pytest.raises(ValueError) as exc_info:
        bearer_posture_from_settings(_ns(audience=_AUDIENCE))
    msg = str(exc_info.value)
    assert "WORKOS_JWKS_URI" in msg
    assert "WORKOS_ISSUER" in msg


# ---------------------------------------------------------------------------
# Partial config — exactly 2 present → ValueError
# ---------------------------------------------------------------------------


def test_jwks_uri_and_issuer_raises():
    """jwks_uri + issuer, no audience → ValueError naming WORKOS_AUDIENCE."""
    with pytest.raises(ValueError) as exc_info:
        bearer_posture_from_settings(_ns(jwks_uri=_JWKS, issuer=_ISSUER))
    assert "WORKOS_AUDIENCE" in str(exc_info.value)


def test_jwks_uri_and_audience_raises():
    """jwks_uri + audience, no issuer → ValueError naming WORKOS_ISSUER."""
    with pytest.raises(ValueError) as exc_info:
        bearer_posture_from_settings(_ns(jwks_uri=_JWKS, audience=_AUDIENCE))
    assert "WORKOS_ISSUER" in str(exc_info.value)


def test_issuer_and_audience_raises():
    """issuer + audience, no jwks_uri → ValueError naming WORKOS_JWKS_URI."""
    with pytest.raises(ValueError) as exc_info:
        bearer_posture_from_settings(_ns(issuer=_ISSUER, audience=_AUDIENCE))
    assert "WORKOS_JWKS_URI" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Whitespace treated as absent
# ---------------------------------------------------------------------------


def test_whitespace_only_treated_as_missing():
    """Whitespace-only issuer with other two set → ValueError naming WORKOS_ISSUER."""
    with pytest.raises(ValueError) as exc_info:
        bearer_posture_from_settings(_ns(jwks_uri=_JWKS, issuer="   ", audience=_AUDIENCE))
    assert "WORKOS_ISSUER" in str(exc_info.value)
