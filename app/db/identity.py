"""User identity resolvers (ADR-015, ADR-049, ADR-053).

HTTP path:  JWT sub claim via validate_token() in the HTTP middleware.
            X-User-Id header is ignored on public HTTP (ADR-049).

Stdio path: MCP_USER_ID env var (operator's WorkOS sub), falling back to
            "default-user" so local dev / legacy stdio sessions keep working.
"""

from __future__ import annotations

import os


def resolve_user_id_stdio() -> str:
    """Resolve user_id for the operator stdio transport.

    Returns the value of MCP_USER_ID (operator's WorkOS sub).  Falls back to
    "default-user" when MCP_USER_ID is absent so that local dev / legacy
    stdio sessions continue to work unchanged.
    """
    return os.environ.get("MCP_USER_ID", "default-user")
