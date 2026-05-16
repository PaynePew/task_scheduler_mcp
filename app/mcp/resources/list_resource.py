"""R1: tasks://list — snapshot of the caller's top 20 jobs, newest-first."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from mcp.server.lowlevel.helper_types import ReadResourceContents
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.engine import async_session_factory as default_session_factory
from app.domain.jobs import list_jobs_resource
from app.mcp.status_mapping import to_external

_MIME = "application/json"


async def read_tasks_list(
    user_id: str,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> list[ReadResourceContents]:
    """Handle resources/read for tasks://list."""
    factory = session_factory or default_session_factory
    snapshot_at = datetime.now(tz=UTC).isoformat()

    async with factory() as session:
        items, total = await list_jobs_resource(session, user_id=user_id, limit=20)

    payload: dict[str, Any] = {
        "snapshot_at": snapshot_at,
        "total": total,
        "items": [
            {
                "job_id": item.job_id,
                "description": item.description,
                "status": to_external(item.internal_status),
                "schedule_type": item.job_type,
                "created_at": item.created_at.isoformat(),
                "cancelled_at": item.cancelled_at.isoformat() if item.cancelled_at else None,
            }
            for item in items
        ],
    }

    return [ReadResourceContents(content=json.dumps(payload), mime_type=_MIME)]
