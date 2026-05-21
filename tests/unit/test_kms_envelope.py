"""Unit tests for app/crypto/kms_envelope (ADR-054).

Uses moto's mock_aws to provide a fake KMS service — no real AWS calls.

Scenarios:
  - encrypt → decrypt round-trip returns original plaintext
  - tampered ciphertext field → DecryptionError
  - tampered wrapped_dek → DecryptionError
  - truncated blob → DecryptionError
  - unknown version → DecryptionError
"""

from __future__ import annotations

import json

import boto3
import pytest
from moto import mock_aws

from app.crypto.kms_envelope import DecryptionError, KmsEnvelope

_REGION = "ap-northeast-1"
_PLAINTEXT = b"super-secret-access-token"


@pytest.fixture()
def kms_client_and_key():
    """Yield a (boto3 kms client, key_id) pair backed by moto's fake KMS."""
    with mock_aws():
        client = boto3.client("kms", region_name=_REGION)
        resp = client.create_key(Description="test-cmk", KeyUsage="ENCRYPT_DECRYPT")
        key_id = resp["KeyMetadata"]["KeyId"]
        yield client, key_id


@pytest.fixture()
def envelope(kms_client_and_key):
    client, key_id = kms_client_and_key
    return KmsEnvelope(kms_client=client, key_id=key_id)


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


def test_encrypt_decrypt_round_trip(envelope):
    blob = envelope.encrypt(_PLAINTEXT)
    assert isinstance(blob, bytes)
    recovered = envelope.decrypt(blob)
    assert recovered == _PLAINTEXT


def test_encrypt_produces_different_blobs_each_call(envelope):
    """Each call generates a fresh data key + fresh nonce → unique blobs."""
    blob1 = envelope.encrypt(_PLAINTEXT)
    blob2 = envelope.encrypt(_PLAINTEXT)
    assert blob1 != blob2


def test_round_trip_empty_bytes(envelope):
    blob = envelope.encrypt(b"")
    assert envelope.decrypt(blob) == b""


def test_round_trip_large_payload(envelope):
    big = b"x" * 100_000
    assert envelope.decrypt(envelope.encrypt(big)) == big


# ---------------------------------------------------------------------------
# Tampered blob → DecryptionError
# ---------------------------------------------------------------------------


def test_tampered_ciphertext_raises(envelope):
    blob = envelope.encrypt(_PLAINTEXT)
    data = json.loads(blob)
    # Flip a byte in the ciphertext
    import base64

    ct = base64.b64decode(data["ciphertext"])
    corrupted = bytes([ct[0] ^ 0xFF]) + ct[1:]
    data["ciphertext"] = base64.b64encode(corrupted).decode()
    bad_blob = json.dumps(data).encode()

    with pytest.raises(DecryptionError):
        envelope.decrypt(bad_blob)


def test_tampered_wrapped_dek_raises(envelope):
    blob = envelope.encrypt(_PLAINTEXT)
    data = json.loads(blob)
    import base64

    dek = base64.b64decode(data["wrapped_dek"])
    corrupted = bytes([dek[0] ^ 0xFF]) + dek[1:]
    data["wrapped_dek"] = base64.b64encode(corrupted).decode()
    bad_blob = json.dumps(data).encode()

    with pytest.raises(DecryptionError):
        envelope.decrypt(bad_blob)


def test_tampered_nonce_raises(envelope):
    blob = envelope.encrypt(_PLAINTEXT)
    data = json.loads(blob)
    import base64

    nonce = base64.b64decode(data["nonce"])
    corrupted = bytes([nonce[0] ^ 0xFF]) + nonce[1:]
    data["nonce"] = base64.b64encode(corrupted).decode()
    bad_blob = json.dumps(data).encode()

    with pytest.raises(DecryptionError):
        envelope.decrypt(bad_blob)


def test_truncated_blob_raises(envelope):
    with pytest.raises(DecryptionError):
        envelope.decrypt(b"not-json")


def test_unknown_version_raises(envelope):
    blob = envelope.encrypt(_PLAINTEXT)
    data = json.loads(blob)
    data["v"] = 99
    bad_blob = json.dumps(data).encode()

    with pytest.raises(DecryptionError):
        envelope.decrypt(bad_blob)


def test_missing_field_raises(envelope):
    blob = envelope.encrypt(_PLAINTEXT)
    data = json.loads(blob)
    del data["ciphertext"]
    bad_blob = json.dumps(data).encode()

    with pytest.raises(DecryptionError):
        envelope.decrypt(bad_blob)
