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
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, assert_never

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.actions.base import ActionResult
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


async def read_upstream(run_id: int, session: AsyncSession, *, user_id: str) -> UpstreamPayload:
    """Fetch upstream JobRun by run_id (scoped to user_id) and return a typed UpstreamPayload.

    Ownership-scoped (IDOR guard): the query filters on JobRun.user_id == user_id,
    so a run owned by a different user is indistinguishable from a missing run —
    it returns NoResult, NOT an error. ``from_run_id`` is a data-plane field that
    bypasses the control-plane ownership check (chain_validation V2), and
    ``JobRun.run_id`` is an enumerable autoincrement PK; returning NoResult (a 404-
    equivalent) rather than a distinct error prevents run_id enumeration, consistent
    with the intentional-404 in chain_validation V2.
    """
    stmt = select(JobRun).where(JobRun.run_id == run_id, JobRun.user_id == user_id)
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


async def resolve_for_display(
    run_id: int,
    session: AsyncSession,
    *,
    formatter: Callable[..., str],
    user_id: str,
) -> str:
    """Resolve upstream and format every variant as a display string.

    Errors are data, never terminal — all 4 variants produce a str.
    The formatter receives (data, is_error=bool); error variants pass
    a pre-built error string as data with is_error=True.

    ``user_id`` scopes the upstream read to the caller's own runs (IDOR guard);
    a cross-tenant run_id resolves to NoResult, formatted as an error string.
    """
    upstream = await read_upstream(run_id, session, user_id=user_id)
    match upstream:
        case Ok(data=data):
            return formatter(data, is_error=False)
        case UpstreamError(error_msg=msg):
            return formatter(msg, is_error=True)
        case NoResult():
            return formatter("(no result)", is_error=True)
        case InvalidJson(raw=raw):
            return formatter(f"(invalid JSON: {raw[:100]})", is_error=True)
        case _:
            assert_never(upstream)


async def resolve_or_terminal(
    run_id: int,
    session: AsyncSession,
    *,
    on_invalid_json: Literal["accept_raw", "fail"],
    user_id: str,
) -> str | ActionResult:
    """Resolve upstream for process-style handlers.

    Ok → raw text (dict/list are JSON-serialized).
    InvalidJson → raw text if on_invalid_json=="accept_raw", else terminal ActionResult.
    UpstreamError / NoResult → terminal ActionResult(ok=False, retryable=False).

    ``user_id`` scopes the upstream read to the caller's own runs (IDOR guard);
    a cross-tenant run_id resolves to NoResult → terminal ActionResult.
    """
    upstream = await read_upstream(run_id, session, user_id=user_id)
    match upstream:
        case Ok(data=data):
            return json.dumps(data) if not isinstance(data, str) else data
        case UpstreamError(error_msg=msg):
            return ActionResult(
                ok=False,
                result=None,
                error=f"Upstream run failed: {msg}",
                retryable=False,
            )
        case NoResult():
            return ActionResult(
                ok=False,
                result=None,
                error="Upstream run has no result",
                retryable=False,
            )
        case InvalidJson(raw=raw):
            if on_invalid_json == "accept_raw":
                return raw
            return ActionResult(
                ok=False,
                result=None,
                error=f"Upstream result is not valid JSON: {raw[:100]}",
                retryable=False,
            )
        case _:
            assert_never(upstream)
