"""Rate limit checker: two-window (daily + burst) per-user limiting via Postgres.

See ADR-042 for design rationale and known limitations.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Job


@dataclass(frozen=True)
class RateLimits:
    daily: int
    burst: int  # per-minute


@dataclass(frozen=True)
class Allow:
    pass


@dataclass(frozen=True)
class Reject:
    reason: str  # "daily" | "burst"
    retry_after_seconds: int


RateLimitDecision = Allow | Reject


async def check_rate_limit(
    user_id: str,
    session: AsyncSession,
    limits: RateLimits,
    *,
    _now: datetime | None = None,
) -> RateLimitDecision:
    """Return Allow or Reject for *user_id* based on two COUNT(*) queries.

    Burst window is checked first (cheaper early-exit for heavy abuse).

    *_now* is injectable for unit-test determinism; production code leaves it None.
    """
    now = _now if _now is not None else datetime.now(UTC)

    # --- burst window (1 minute) ---
    burst_start = now - timedelta(minutes=1)
    burst_row = (
        await session.execute(
            select(func.count(), func.min(Job.created_at))
            .select_from(Job)
            .where(Job.user_id == user_id)
            .where(Job.created_at > burst_start)
        )
    ).one()
    burst_count, burst_oldest = burst_row
    if burst_count >= limits.burst:
        retry_after = max(1, math.ceil((burst_oldest + timedelta(minutes=1) - now).total_seconds()))
        return Reject(reason="burst", retry_after_seconds=retry_after)

    # --- daily window (24 hours) ---
    daily_start = now - timedelta(hours=24)
    daily_row = (
        await session.execute(
            select(func.count(), func.min(Job.created_at))
            .select_from(Job)
            .where(Job.user_id == user_id)
            .where(Job.created_at > daily_start)
        )
    ).one()
    daily_count, daily_oldest = daily_row
    if daily_count >= limits.daily:
        retry_after = max(1, math.ceil((daily_oldest + timedelta(hours=24) - now).total_seconds()))
        return Reject(reason="daily", retry_after_seconds=retry_after)

    return Allow()
