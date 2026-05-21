"""Per-process KmsEnvelope factory (ADR-054).

Provides a cached KmsEnvelope built from settings so callers share one boto3
client per process rather than constructing one per handler init.  Returns None
when KMS is not configured (parity with today's behaviour).
"""

from __future__ import annotations

from app.crypto.kms_envelope import KmsEnvelope

_UNSET: object = object()
_cached: KmsEnvelope | None | object = _UNSET


def kms_envelope_from_settings() -> KmsEnvelope | None:
    """Return a process-level cached KmsEnvelope, or None if KMS is absent.

    On first call, reads settings.kms_key_id; if falsy, caches and returns None.
    Subsequent calls return the cached value without re-reading settings.
    """
    global _cached
    if _cached is _UNSET:
        _cached = _build()
    return _cached  # type: ignore[return-value]


def _build() -> KmsEnvelope | None:
    from app.config.settings import settings  # noqa: PLC0415

    if not settings.kms_key_id:
        return None
    import boto3  # noqa: PLC0415

    client = boto3.client(
        "kms",
        region_name=settings.kms_region,
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
    )
    return KmsEnvelope(kms_client=client, key_id=settings.kms_key_id)
