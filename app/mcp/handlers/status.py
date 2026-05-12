"""task.status@v1 MCP handler."""

from __future__ import annotations

import logging
from typing import Any

from app.db.engine import async_session_factory
from app.domain.jobs import JobNotFoundError, get_job_with_runs
from app.mcp.envelope import error, success
from app.mcp.status_mapping import to_external

logger = logging.getLogger(__name__)

TASK_STATUS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "job_id": {
            "type": "integer",
            "description": "The job ID returned by task.create@v1.",
        },
        "include_runs": {
            "type": "boolean",
            "default": False,
            "description": "If true, return the most recent 10 execution runs.",
        },
    },
    "required": ["job_id"],
    "additionalProperties": False,
}


async def handle_task_status(
    arguments: dict[str, Any],
    user_id: str,
) -> dict[str, Any]:
    """Handle task.status@v1 — returns job status and optionally recent runs.

    Returns NOT_FOUND for unknown or cross-user job_id per ADR-014.
    """
    job_id_raw = arguments.get("job_id")
    include_runs = bool(arguments.get("include_runs", False))

    try:
        job_id = int(job_id_raw)
    except (TypeError, ValueError):
        return error(
            "USER_INPUT",
            "job_id must be an integer",
            field="job_id",
            expected="integer",
        )

    try:
        async with async_session_factory() as session:
            view = await get_job_with_runs(
                session,
                user_id=user_id,
                job_id=job_id,
                include_runs=include_runs,
            )
    except JobNotFoundError:
        return error("NOT_FOUND", f"Job {job_id} not found")
    except Exception:
        logger.exception("task.status@v1 failed for user %s job %s", user_id, job_id)
        return error("INTERNAL", "An unexpected error occurred.")

    data: dict[str, Any] = {
        "job_id": view.job_id,
        "action": view.action,
        "status": to_external(view.internal_status),
    }

    if include_runs and view.runs is not None:
        data["runs"] = [
            {
                "run_id": r.run_id,
                "status": to_external(r.status),
                "scheduled_at": r.scheduled_at.isoformat(),
                "start_at": r.start_at.isoformat() if r.start_at else None,
                "finish_at": r.finish_at.isoformat() if r.finish_at else None,
            }
            for r in view.runs
        ]

    return success(data)
