"""Bearer-token verification posture — mechanism-named sum type (ADR-053).

BearerVerifyPosture = TrustOnly | BearerVerified

TrustOnly   — no JWT check; any presented user identity is trusted (local dev / CI).
BearerVerified — verify incoming tokens against JWKS at jwks_uri.

Use bearer_posture_from_settings() to construct the posture from Settings fields.
Raises ValueError on partial WorkOS config so the caller (Settings validator)
can fail fast rather than silently degrading to trust-only mode.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class _WorkosFields(Protocol):
    workos_jwks_uri: str | None
    workos_issuer: str | None
    workos_audience: str | None


@dataclass(frozen=True)
class TrustOnly:
    """No bearer verification — trust any presented identity."""


@dataclass(frozen=True)
class BearerVerified:
    """Verify bearer tokens against a JWKS endpoint."""

    jwks_uri: str
    issuer: str
    audience: str


BearerVerifyPosture = TrustOnly | BearerVerified


def _present(val: str | None) -> bool:
    return bool(val and val.strip())


def bearer_posture_from_settings(s: _WorkosFields) -> BearerVerifyPosture:
    """Construct the posture from WorkOS-related settings fields.

    Returns TrustOnly when all three fields are absent/empty, BearerVerified
    when all three are present, and raises ValueError on any partial subset.
    """
    fields = [
        ("WORKOS_JWKS_URI", s.workos_jwks_uri),
        ("WORKOS_ISSUER", s.workos_issuer),
        ("WORKOS_AUDIENCE", s.workos_audience),
    ]
    missing = [name for name, val in fields if not _present(val)]

    if len(missing) == len(fields):
        return TrustOnly()

    if not missing:
        # All three present — the type: ignore is safe because _present() verified non-empty.
        return BearerVerified(
            jwks_uri=s.workos_jwks_uri,  # type: ignore[arg-type]
            issuer=s.workos_issuer,  # type: ignore[arg-type]
            audience=s.workos_audience,  # type: ignore[arg-type]
        )

    raise ValueError(
        f"Partial WorkOS configuration detected. "
        f"Missing: {', '.join(missing)}. "
        f"Either set all three (WORKOS_JWKS_URI, WORKOS_ISSUER, WORKOS_AUDIENCE) "
        f"for auth-enabled mode, or leave all three unset for trust-only dev mode."
    )
