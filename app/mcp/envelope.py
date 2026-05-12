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
    expected: str | None = None,
) -> dict[str, Any]:
    """Wrap an error result: {"ok": false, "error": {"code", "message", ...}}."""
    err: dict[str, Any] = {"code": code, "message": message}
    if field is not None:
        err["field"] = field
    if expected is not None:
        err["expected"] = expected
    return {"ok": False, "error": err}
