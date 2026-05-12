"""User identity resolver — fallback chain per ADR-015."""

import os


def resolve_user_id(x_user_id: str | None = None) -> str:
    """Resolve user_id: X-User-Id header → MCP_USER_ID env → "default-user"."""
    if x_user_id:
        return x_user_id
    env_val = os.environ.get("MCP_USER_ID")
    if env_val:
        return env_val
    return "default-user"
