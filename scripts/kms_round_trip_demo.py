#!/usr/bin/env python
"""Admin/CLI demo: KMS envelope encryption round-trip against the configured CMK.

Exercises the encrypt → decrypt path of app/crypto/kms_envelope against a real
AWS KMS CMK.  Requires KMS_KEY_ID (and optionally AWS_ACCESS_KEY_ID /
AWS_SECRET_ACCESS_KEY / KMS_REGION) to be set in the environment or .env file.

Usage:
    uv run python scripts/kms_round_trip_demo.py
    uv run python scripts/kms_round_trip_demo.py --plaintext "my test payload"

Exit codes:
    0  — encrypt → decrypt succeeded; recovered plaintext matches input
    1  — configuration error (missing KMS_KEY_ID)
    2  — round-trip verification failed (encrypt/decrypt mismatch or KMS error)
"""

from __future__ import annotations

import argparse
import sys

import boto3

# Load .env if present (same mechanism as settings.py)
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass  # python-dotenv not required; env vars sourced from the environment directly

from app.config.settings import settings
from app.crypto.kms_envelope import DecryptionError, KmsEnvelope


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="KMS envelope encryption round-trip demo")
    parser.add_argument(
        "--plaintext",
        default="hello from the connection store",
        help="Payload to encrypt then decrypt (default: a fixed test string)",
    )
    args = parser.parse_args(argv)

    if not settings.kms_key_id:
        print(
            "ERROR: KMS_KEY_ID is not configured.\n"
            "Set it in .env or export KMS_KEY_ID=<arn-or-alias> before running.",
            file=sys.stderr,
        )
        return 1

    print(f"KMS region : {settings.kms_region}")
    print(f"CMK key id : {settings.kms_key_id}")
    print(f"Plaintext  : {args.plaintext!r}")
    print()

    kms_kwargs: dict = {"region_name": settings.kms_region}
    if settings.aws_access_key_id and settings.aws_secret_access_key:
        kms_kwargs["aws_access_key_id"] = settings.aws_access_key_id
        kms_kwargs["aws_secret_access_key"] = settings.aws_secret_access_key

    client = boto3.client("kms", **kms_kwargs)
    envelope = KmsEnvelope(kms_client=client, key_id=settings.kms_key_id)

    plaintext_bytes = args.plaintext.encode()

    print("Encrypting …")
    try:
        blob = envelope.encrypt(plaintext_bytes)
    except Exception as exc:
        print(f"ENCRYPT FAILED: {exc}", file=sys.stderr)
        return 2

    print(f"  blob length : {len(blob)} bytes")
    print()

    print("Decrypting …")
    try:
        recovered = envelope.decrypt(blob)
    except DecryptionError as exc:
        print(f"DECRYPT FAILED: {exc}", file=sys.stderr)
        return 2

    print(f"  recovered   : {recovered.decode()!r}")
    print()

    if recovered != plaintext_bytes:
        print("MISMATCH: recovered plaintext does not match input!", file=sys.stderr)
        return 2

    print("Round-trip OK — recovered plaintext matches input.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
