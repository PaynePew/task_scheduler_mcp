"""Envelope formatters for MCP tool responses per ADR-014."""

from __future__ import annotations

from typing import Any


def success(data: dict[str, Any]) -> dict[str, Any]:
    """Wrap a successful result: {"ok": true, "data": {...}}."""
    return {"ok": True, "data": data}


def error(
    code: str,
    message: str,
    field: str | None = None,
    expected: Any = None,
    connect_url: str | None = None,
) -> dict[str, Any]:
    """Wrap an error result: {"ok": false, "error": {"code", "message", ...}}.

    connect_url is included when an action requires an OAuth connection that the
    user has not yet set up (ADR-058) — surfaced as a direct link to /connections.
    """
    err: dict[str, Any] = {"code": code, "message": message}
    if field is not None:
        err["field"] = field
    if expected is not None:
        err["expected"] = expected
    if connect_url is not None:
        err["connect_url"] = connect_url
    return {"ok": False, "error": err}
