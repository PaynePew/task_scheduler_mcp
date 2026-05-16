"""Timezone resolver: 4-step fallback chain (ADR-017).

Priority order (first valid IANA key wins):
  1. call_value   — explicit timezone argument passed in the tool call
  2. header_value — value from the request header (e.g. X-Timezone)
  3. env_value    — process environment variable (e.g. MCP_USER_TZ)
  4. "UTC"        — unconditional fallback

Only IANA keys accepted by ``zoneinfo.ZoneInfo`` are considered valid.
Offset strings (``+08:00``, ``UTC+8``) and Windows IDs are rejected.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _is_valid_iana(value: str | None) -> bool:
    """Return True if *value* is a non-empty, resolvable IANA timezone key."""
    if not value:
        return False
    try:
        ZoneInfo(value)
        return True
    except (ZoneInfoNotFoundError, KeyError):
        return False


def resolve_timezone(
    call_value: str | None,
    header_value: str | None,
    env_value: str | None,
) -> str:
    """Return the first valid IANA timezone name in the 4-step priority chain.

    Falls back to ``"UTC"`` if none of the provided values is a valid IANA key.
    """
    for candidate in (call_value, header_value, env_value):
        if _is_valid_iana(candidate):
            return candidate  # type: ignore[return-value]
    return "UTC"
