"""Deep module for reading upstream JobRun.result in chained handlers.

Any handler whose params_model includes ``from_run_id: int | None`` calls
``read_upstream`` to fetch and parse the upstream result before dispatching
on the returned variant.  See ADR-033 for the full convention.

Dispatch rules:
  run not found           → NoResult
  result=NULL, no error   → NoResult
  result=NULL, error set  → UpstreamError(error_message)
  result is valid JSON    → Ok(parsed_data)
  result is invalid JSON  → InvalidJson(raw_string)
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import JobRun


@dataclass(frozen=True)
class Ok:
    """Upstream result exists and was parsed as JSON."""

    data: Any


@dataclass(frozen=True)
class UpstreamError:
    """Upstream run has error_message set (failed run with diagnostic info)."""

    error_msg: str


@dataclass(frozen=True)
class NoResult:
    """Upstream run not found, or result is NULL with no error_message."""


@dataclass(frozen=True)
class InvalidJson:
    """Upstream result is non-NULL but not valid JSON."""

    raw: str


UpstreamPayload = Ok | UpstreamError | NoResult | InvalidJson


async def read_upstream(run_id: int, session: AsyncSession) -> UpstreamPayload:
    """Fetch upstream JobRun by run_id and return a typed UpstreamPayload."""
    stmt = select(JobRun).where(JobRun.run_id == run_id)
    run: JobRun | None = (await session.execute(stmt)).scalars().first()

    if run is None:
        return NoResult()

    if run.result is None:
        if run.error_message:
            return UpstreamError(run.error_message)
        return NoResult()

    try:
        parsed = json.loads(run.result)
        return Ok(parsed)
    except (json.JSONDecodeError, ValueError):
        return InvalidJson(run.result)
