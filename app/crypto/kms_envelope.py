"""KMS envelope encryption for OAuth token storage (ADR-054).

Per-write data key flow:
  encrypt: GenerateDataKey → AES-256-GCM encrypt plaintext → persist
           {ciphertext, wrapped_dek, nonce}
  decrypt: KMS Decrypt wrapped_dek → AES-256-GCM decrypt ciphertext

The CMK material never leaves KMS. The plaintext data key is held only
in process memory for the duration of a single encrypt call.

Wire format (JSON, base64-encoded fields):
    {
        "v": 1,
        "wrapped_dek": "<base64>",
        "nonce": "<base64>",
        "ciphertext": "<base64>"
    }
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_VERSION = 1
_NONCE_BYTES = 12  # 96-bit nonce for AES-GCM (NIST recommended)


class DecryptionError(Exception):
    """Raised when a blob cannot be decrypted (tampered, wrong key, etc.)."""


class KmsEnvelope:
    """Encrypts / decrypts arbitrary bytes using AWS KMS envelope encryption.

    Args:
        kms_client: A boto3 KMS client (or compatible fake for testing).
        key_id: KMS CMK ARN or alias (e.g. ``alias/scheduler-token-key``).
    """

    def __init__(self, kms_client: Any, key_id: str) -> None:
        self._kms = kms_client
        self._key_id = key_id

    def encrypt(self, plaintext: bytes) -> bytes:
        """Encrypt *plaintext* and return a serialized blob.

        Calls KMS GenerateDataKey once per invocation; the plaintext data
        key is discarded after the local AES-GCM operation.
        """
        resp = self._kms.generate_data_key(KeyId=self._key_id, KeySpec="AES_256")
        plaintext_dek: bytes = resp["Plaintext"]
        wrapped_dek: bytes = resp["CiphertextBlob"]

        nonce = os.urandom(_NONCE_BYTES)
        aesgcm = AESGCM(plaintext_dek)
        ciphertext = aesgcm.encrypt(nonce, plaintext, None)

        # Drop our reference; the bytes object is left for GC (CPython del
        # does not zero memory — a true wipe would need bytearray + ctypes.memset).
        del plaintext_dek

        blob = json.dumps(
            {
                "v": _VERSION,
                "wrapped_dek": base64.b64encode(wrapped_dek).decode(),
                "nonce": base64.b64encode(nonce).decode(),
                "ciphertext": base64.b64encode(ciphertext).decode(),
            }
        ).encode()
        return blob

    def decrypt(self, blob: bytes) -> bytes:
        """Decrypt a blob produced by :meth:`encrypt`.

        Raises:
            DecryptionError: If the blob is malformed, tampered, or the KMS
                call fails.
        """
        try:
            data = json.loads(blob)
            if data.get("v") != _VERSION:
                raise DecryptionError(f"Unknown blob version: {data.get('v')!r}")

            wrapped_dek = base64.b64decode(data["wrapped_dek"])
            nonce = base64.b64decode(data["nonce"])
            ciphertext = base64.b64decode(data["ciphertext"])
        except (KeyError, ValueError, TypeError) as exc:
            raise DecryptionError(f"Malformed blob: {exc}") from exc

        try:
            resp = self._kms.decrypt(
                CiphertextBlob=wrapped_dek,
                KeyId=self._key_id,
            )
            plaintext_dek: bytes = resp["Plaintext"]
        except Exception as exc:
            raise DecryptionError(f"KMS decrypt failed: {exc}") from exc

        try:
            aesgcm = AESGCM(plaintext_dek)
            plaintext = aesgcm.decrypt(nonce, ciphertext, None)
        except Exception as exc:
            raise DecryptionError(f"AES-GCM authentication failed: {exc}") from exc
        finally:
            # Drop our reference; the bytes object is left for GC (CPython del
            # does not zero memory — a true wipe would need bytearray + ctypes.memset).
            del plaintext_dek

        return plaintext
