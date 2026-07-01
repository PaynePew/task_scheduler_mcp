"""task.list.v1 MCP handler."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.engine import async_session_factory as default_session_factory
from app.domain.jobs import list_jobs
from app.mcp.envelope import error, success
from app.mcp.status_mapping import to_external_job_status, to_internal_set

logger = logging.getLogger(__name__)

_VALID_EXTERNAL_STATUSES = frozenset({"scheduled", "running", "completed", "failed", "cancelled"})

TASK_LIST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": ["string", "null"],
            "enum": ["scheduled", "running", "completed", "failed", "cancelled", None],
            "default": None,
            "description": "Filter by external status. Omit to return all statuses.",
        },
        "created_at_from": {
            "type": ["string", "null"],
            "default": None,
            "description": "ISO 8601 timezone-aware datetime; jobs created at or after this time.",
        },
        "created_at_to": {
            "type": ["string", "null"],
            "default": None,
            "description": "ISO 8601 timezone-aware datetime; jobs created at or before this time.",
        },
        "page": {
            "type": "integer",
            "default": 1,
            "minimum": 1,
            "description": "1-based page number.",
        },
        "pageSize": {
            "type": "integer",
            "default": 20,
            "minimum": 1,
            "maximum": 100,
            "description": "Items per page (1–100).",
        },
    },
    "additionalProperties": False,
}


def _parse_tz_datetime(value: Any, field_name: str) -> datetime | dict[str, Any]:
    """Parse an ISO 8601 string into a timezone-aware datetime.

    Returns the parsed datetime on success, or a USER_INPUT error envelope on failure.
    """
    if not isinstance(value, str):
        return error("USER_INPUT", f"{field_name} must be an ISO 8601 string", field=field_name)
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return error(
            "USER_INPUT",
            f"{field_name} is not a valid ISO 8601 datetime",
            field=field_name,
            expected="ISO 8601 timezone-aware datetime",
        )
    if dt.tzinfo is None:
        return error(
            "USER_INPUT",
            f"{field_name} must include a timezone (e.g. '2026-01-01T00:00:00Z')",
            field=field_name,
            expected="ISO 8601 timezone-aware datetime",
        )
    return dt


async def handle_task_list(
    arguments: dict[str, Any],
    user_id: str,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> dict[str, Any]:
    """Handle task.list.v1 — returns paged jobs for the caller, newest-first.

    `session_factory` is injectable for testing (same rationale as status/cancel handlers).
    """
    factory = session_factory or default_session_factory

    # --- pagination validation ---
    page_raw = arguments.get("page", 1)
    page_size_raw = arguments.get("pageSize", 20)

    try:
        page = int(page_raw)
    except (TypeError, ValueError):
        page = 0
    try:
        page_size = int(page_size_raw)
    except (TypeError, ValueError):
        page_size = 0

    if page < 1:
        return error("USER_INPUT", "page must be >= 1", field="page", expected="1..")
    if page_size < 1:
        return error("USER_INPUT", "pageSize must be >= 1", field="pageSize", expected="1..100")
    if page_size > 100:
        return error("USER_INPUT", "pageSize must be <= 100", field="pageSize", expected="1..100")

    # --- status filter ---
    status_raw = arguments.get("status")
    internal_statuses: frozenset[str] | None = None
    if status_raw is not None:
        if status_raw not in _VALID_EXTERNAL_STATUSES:
            return error(
                "USER_INPUT",
                f"status {status_raw!r} is not a valid external status",
                field="status",
                expected=", ".join(sorted(_VALID_EXTERNAL_STATUSES)),
            )
        internal_statuses = to_internal_set(status_raw)

    # --- created_at range ---
    created_at_from: datetime | None = None
    created_at_to: datetime | None = None

    from_raw = arguments.get("created_at_from")
    if from_raw is not None:
        result = _parse_tz_datetime(from_raw, "created_at_from")
        if isinstance(result, dict):
            return result
        created_at_from = result

    to_raw = arguments.get("created_at_to")
    if to_raw is not None:
        result = _parse_tz_datetime(to_raw, "created_at_to")
        if isinstance(result, dict):
            return result
        created_at_to = result

    # --- query ---
    try:
        async with factory() as session:
            paged = await list_jobs(
                session,
                user_id=user_id,
                status_filter=internal_statuses,
                created_at_from=created_at_from,
                created_at_to=created_at_to,
                page=page,
                page_size=page_size,
            )
    except Exception:
        logger.exception("task.list.v1 failed for user %s", user_id)
        return error("INTERNAL", "An unexpected error occurred.")

    return success(
        {
            "jobs": [
                {
                    "job_id": item.job_id,
                    "action": item.action,
                    "status": to_external_job_status(
                        job_state=item.job_state, latest_run_status=item.latest_run_status
                    ),
                    "created_at": item.created_at.isoformat(),
                }
                for item in paged.items
            ],
            "total": paged.total,
            "page": paged.page,
            "pageSize": paged.page_size,
        }
    )
