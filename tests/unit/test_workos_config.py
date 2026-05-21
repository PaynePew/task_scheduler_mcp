"""Smoke tests: Settings construction raises ValidationError on partial WorkOS config.

Two thin integration checks confirming the Pydantic shim still rejects partial
configs through the Settings layer. Detailed factory logic lives in
tests/unit/auth/test_posture.py.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError


def _make_settings(**kw):
    from app.config.settings import Settings

    return Settings(_env_file=None, **kw)


def test_partial_none_raises_validation_error():
    """One var set with others None → Settings raises ValidationError."""
    with pytest.raises(ValidationError) as exc_info:
        _make_settings(workos_jwks_uri="https://api.workos.com/sso/jwks/client_123")
    assert "WORKOS_ISSUER" in str(exc_info.value)
    assert "WORKOS_AUDIENCE" in str(exc_info.value)


def test_partial_empty_string_raises_validation_error():
    """One real value + empty strings → Settings raises ValidationError."""
    with pytest.raises(ValidationError) as exc_info:
        _make_settings(
            workos_jwks_uri="https://api.workos.com/sso/jwks/client_123",
            workos_issuer="https://api.workos.com",
            workos_audience="",
        )
    assert "WORKOS_AUDIENCE" in str(exc_info.value)
