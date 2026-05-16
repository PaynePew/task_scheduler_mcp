"""R2: tasks://job/{job_id} — single job with 5 recent runs."""

from __future__ import annotations

import json
import logging

from mcp import McpError
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.types import INVALID_PARAMS, ErrorData
from pydantic import AnyUrl
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.engine import async_session_factory as default_session_factory
from app.domain.jobs import JobNotFoundError, get_job_with_runs
from app.mcp.status_mapping import to_external

logger = logging.getLogger(__name__)

_MIME = "application/json"


def _extract_job_id(uri: AnyUrl) -> int:
    """Parse job_id from tasks://job/{job_id}. Raises McpError on bad input."""
    path = uri.path  # e.g. "/123"
    if not path:
        raise McpError(ErrorData(code=INVALID_PARAMS, message=f"Invalid resource URI: {uri}"))
    try:
        return int(path.lstrip("/"))
    except ValueError:
        raise McpError(
            ErrorData(code=INVALID_PARAMS, message=f"Invalid job_id in URI: {uri}")
        ) from None


async def read_tasks_job(
    uri: AnyUrl,
    user_id: str,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> list[ReadResourceContents]:
    """Handle resources/read for tasks://job/{job_id}."""
    factory = session_factory or default_session_factory
    job_id = _extract_job_id(uri)

    try:
        async with factory() as session:
            view = await get_job_with_runs(
                session,
                user_id=user_id,
                job_id=job_id,
                include_runs=True,
                run_limit=5,
            )
    except JobNotFoundError:
        raise McpError(ErrorData(code=INVALID_PARAMS, message=f"Job {job_id} not found")) from None
    except Exception:
        logger.exception("resources/read tasks://job/%s failed for user %s", job_id, user_id)
        raise McpError(
            ErrorData(code=INVALID_PARAMS, message="An unexpected error occurred")
        ) from None

    payload = {
        "job_id": view.job_id,
        "description": view.description,
        "action": view.action,
        "schedule_type": view.job_type,
        "created_at": view.created_at.isoformat() if view.created_at else None,
        "status": to_external(view.internal_status),
        "runs": [
            {
                "run_id": r.run_id,
                "status": to_external(r.status),
                "scheduled_at": r.scheduled_at.isoformat(),
                "start_at": r.start_at.isoformat() if r.start_at else None,
                "finish_at": r.finish_at.isoformat() if r.finish_at else None,
            }
            for r in (view.runs or [])
        ],
    }

    return [ReadResourceContents(content=json.dumps(payload), mime_type=_MIME)]
