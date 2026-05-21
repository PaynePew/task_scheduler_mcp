"""Unit tests for app.crypto.envelope_factory — kms_envelope_from_settings().

Scenarios:
  - returns None when settings.kms_key_id is falsy
  - None result is cached (second call returns same object, _build not re-called)
  - KmsEnvelope result is cached (second call returns same instance)
  - cache is populated after first call
"""

from __future__ import annotations

import boto3
import pytest
from moto import mock_aws

import app.crypto.envelope_factory as factory_module
from app.crypto.envelope_factory import kms_envelope_from_settings
from app.crypto.kms_envelope import KmsEnvelope

_REGION = "ap-northeast-1"


@pytest.fixture(autouse=True)
def reset_cache(monkeypatch):
    """Reset the module-level cache before each test."""
    monkeypatch.setattr(factory_module, "_cached", factory_module._UNSET)


# ---------------------------------------------------------------------------
# None-return when KMS is not configured
# ---------------------------------------------------------------------------


def test_returns_none_when_kms_key_id_absent():
    """Returns None when settings.kms_key_id is falsy (default in test env)."""
    result = kms_envelope_from_settings()
    assert result is None


# ---------------------------------------------------------------------------
# Cache behaviour
# ---------------------------------------------------------------------------


def test_none_result_is_cached(monkeypatch):
    """Second call returns the same None without calling _build() again."""
    build_calls: list[int] = []

    original_build = factory_module._build

    def counting_build() -> KmsEnvelope | None:
        build_calls.append(1)
        return original_build()

    monkeypatch.setattr(factory_module, "_build", counting_build)

    first = kms_envelope_from_settings()
    second = kms_envelope_from_settings()

    assert first is None
    assert second is None
    assert len(build_calls) == 1, "_build() must be called exactly once"


def test_envelope_result_is_cached(monkeypatch):
    """Returns the same KmsEnvelope instance on repeated calls when KMS is configured."""
    with mock_aws():
        kms_client = boto3.client("kms", region_name=_REGION)
        key_id = kms_client.create_key(Description="test", KeyUsage="ENCRYPT_DECRYPT")[
            "KeyMetadata"
        ]["KeyId"]

        fake_envelope = KmsEnvelope(kms_client=kms_client, key_id=key_id)
        monkeypatch.setattr(factory_module, "_build", lambda: fake_envelope)

        first = kms_envelope_from_settings()
        second = kms_envelope_from_settings()

    assert first is second
    assert first is fake_envelope


def test_cache_populated_after_call():
    """After first call, _cached is no longer _UNSET."""
    assert factory_module._cached is factory_module._UNSET
    kms_envelope_from_settings()
    assert factory_module._cached is not factory_module._UNSET
