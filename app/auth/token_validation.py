"""JWT Bearer-token validation for the OAuth 2.1 resource server (ADR-053).

validate_token() verifies:
  - signature against the JWKS at ``jwks_uri``
  - ``exp`` (not expired)
  - ``aud`` matches ``audience``
  - ``iss`` matches ``issuer``
  - ``sub`` is present (becomes user_id)

The _jwks_client parameter is injectable so unit tests can supply a pre-built
PyJWKClient backed by fixture keys without making real network requests.
"""

from __future__ import annotations

from dataclasses import dataclass

import jwt
from jwt import PyJWKClient


@dataclass(frozen=True)
class AuthContext:
    user_id: str


class TokenValidationError(Exception):
    """Raised when a Bearer token cannot be validated."""


_ALGORITHMS = ["RS256", "RS384", "RS512", "ES256", "ES384", "ES512"]


def validate_token(
    token: str,
    *,
    issuer: str,
    audience: str,
    jwks_uri: str,
    _jwks_client: PyJWKClient | None = None,
) -> AuthContext:
    """Validate *token* and return an AuthContext with the verified sub.

    Args:
        token: Raw Bearer token string (without the ``Bearer `` prefix).
        issuer: Expected ``iss`` claim (WorkOS issuer URL).
        audience: Expected ``aud`` claim (our resource identifier, RFC 8707).
        jwks_uri: URL of the JWKS endpoint used to fetch signing keys.
        _jwks_client: Optional pre-built client; supplied by tests to avoid
            live network calls.  Production code never passes this.

    Raises:
        TokenValidationError: On any validation failure (expired, bad signature,
            wrong audience, missing sub, etc.).
    """
    client = _jwks_client or PyJWKClient(jwks_uri, cache_keys=True)
    try:
        signing_key = client.get_signing_key_from_jwt(token)
        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=_ALGORITHMS,
            audience=audience,
            issuer=issuer,
            options={"require": ["exp", "iat", "sub", "aud", "iss"]},
        )
    except jwt.InvalidTokenError as exc:
        raise TokenValidationError(str(exc)) from exc

    sub = payload.get("sub")
    if not sub:
        raise TokenValidationError("token missing 'sub' claim")

    return AuthContext(user_id=sub)
