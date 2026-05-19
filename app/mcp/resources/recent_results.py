"""R4: tasks://recent-results — last 24h of terminal JobRuns for the calling user.

Closes the silent-success loop: scheduled jobs fire while the LLM client is
closed; when the user reopens Claude Desktop, the client surfaces this resource
on connect and can proactively brief the user on overnight activity (ADR-037).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from mcp.server.lowlevel.helper_types import ReadResourceContents
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.engine import async_session_factory as default_session_factory
from app.db.models import Job, JobRun
from app.mcp.status_mapping import to_external

_MIME = "application/json"
_WINDOW_HOURS = 24
_MAX_ROWS = 50
_TERMINAL_STATUSES = frozenset({"SUCCEEDED", "FAILED", "CANCELLED"})
_ERROR_EXCERPT_MAX = 200


async def read_tasks_recent_results(
    user_id: str,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> list[ReadResourceContents]:
    """Handle resources/read for tasks://recent-results."""
    factory = session_factory or default_session_factory
    queried_at = datetime.now(tz=UTC)
    cutoff = queried_at - timedelta(hours=_WINDOW_HOURS)

    async with factory() as session:
        runs = await _query_recent_runs(session, user_id=user_id, cutoff=cutoff)

    payload: dict[str, Any] = {
        "queried_at": queried_at.isoformat(),
        "user_id": user_id,
        "window_hours": _WINDOW_HOURS,
        "runs": [
            {
                "job_id": r.job_id,
                "run_id": r.run_id,
                "action": r.action,
                "status": to_external(r.status),
                "start_at": r.start_at.isoformat() if r.start_at else None,
                "finish_at": r.finish_at.isoformat() if r.finish_at else None,
                "error_excerpt": (r.error_message or "")[:_ERROR_EXCERPT_MAX] or None,
            }
            for r in runs[:_MAX_ROWS]
        ],
    }

    return [ReadResourceContents(content=json.dumps(payload), mime_type=_MIME)]


async def _query_recent_runs(
    session: AsyncSession,
    *,
    user_id: str,
    cutoff: datetime,
) -> list[Any]:
    """Return at most 50 terminal JobRuns for user_id with finish_at >= cutoff."""
    stmt = (
        select(
            JobRun.run_id,
            JobRun.job_id,
            JobRun.status,
            JobRun.start_at,
            JobRun.finish_at,
            JobRun.error_message,
            Job.action,
        )
        .join(Job, Job.job_id == JobRun.job_id)
        .where(
            Job.user_id == user_id,
            JobRun.status.in_(list(_TERMINAL_STATUSES)),
            JobRun.finish_at >= cutoff,
        )
        .order_by(JobRun.finish_at.desc())
        .limit(_MAX_ROWS)
    )
    result = await session.execute(stmt)
    return result.all()
