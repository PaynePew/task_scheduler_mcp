"""task.cancel@v1 MCP handler."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.engine import async_session_factory as default_session_factory
from app.domain.jobs import InvalidStateError, JobNotFoundError, cancel_job
from app.mcp.envelope import error, success
from app.mcp.status_mapping import to_external

logger = logging.getLogger(__name__)

TASK_CANCEL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "job_id": {
            "type": "integer",
            "description": "The job ID returned by task.create@v1.",
        },
    },
    "required": ["job_id"],
    "additionalProperties": False,
}


async def handle_task_cancel(
    arguments: dict[str, Any],
    user_id: str,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> dict[str, Any]:
    """Handle task.cancel@v1 — cancel all non-terminal runs for a job.

    Returns NOT_FOUND for unknown or cross-user job_id.
    Returns INVALID_STATE when all runs are already terminal.

    `session_factory` is injectable so tests can pass a per-test factory.
    """
    factory = session_factory or default_session_factory

    job_id_raw = arguments.get("job_id")

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
        async with factory() as session:
            view = await cancel_job(session, user_id=user_id, job_id=job_id)
    except JobNotFoundError:
        return error("NOT_FOUND", f"Job {job_id} not found")
    except InvalidStateError as exc:
        external = to_external(str(exc))
        return error(
            "INVALID_STATE",
            f"Job {job_id} cannot be cancelled; current status is {external!r}",
        )
    except Exception:
        logger.exception("task.cancel@v1 failed for user %s job %s", user_id, job_id)
        return error("INTERNAL", "An unexpected error occurred.")

    return success({"job_id": view.job_id, "status": to_external(view.internal_status)})
