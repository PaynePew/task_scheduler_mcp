"""Containment caps at task.create: per-user active counts and global ceiling.

See ADR-055 for design rationale. Three caps enforced in order:
  1. Active recurring per user — bounds permanent steady-state load.
  2. Active total per user — prevents one user hoarding the box.
  3. Global active recurring ceiling — protects the single core.

'Active' is defined as: active=True AND cancelled_at IS NULL.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Job

# Seconds to suggest retrying when a count-based cap is hit.
# No natural window expiry — the cap lifts when other jobs are cancelled.
_RETRY_AFTER = 60


@dataclass(frozen=True)
class ContainmentLimits:
    active_recurring_per_user: int
    active_total_per_user: int
    global_active_recurring: int


@dataclass(frozen=True)
class Allow:
    pass


@dataclass(frozen=True)
class Reject:
    reason: str  # "active_recurring_per_user" | "active_total_per_user" | "global_active_recurring"
    retry_after_seconds: int


ContainmentDecision = Allow | Reject


async def check_containment(
    user_id: str,
    session: AsyncSession,
    limits: ContainmentLimits,
    *,
    is_operator: bool = False,
) -> ContainmentDecision:
    """Return Allow or Reject for *user_id* based on active-job containment caps.

    Pass *is_operator=True* to bypass all caps (OPERATOR_USER_ID exemption, ADR-055).

    Checks in order — earliest rejection exits cheaply:
      1. active recurring per user (uses idx_jobs_active_recurring partial index)
      2. active total per user
      3. global active recurring ceiling
    """
    if is_operator:
        return Allow()

    # 1. Active recurring per user
    recurring_count: int = (
        await session.execute(
            select(func.count())
            .select_from(Job)
            .where(Job.user_id == user_id)
            .where(Job.active.is_(True))
            .where(Job.job_type == "recurring")
            .where(Job.cancelled_at.is_(None))
        )
    ).scalar_one()
    if recurring_count >= limits.active_recurring_per_user:
        return Reject(reason="active_recurring_per_user", retry_after_seconds=_RETRY_AFTER)

    # 2. Active total per user
    total_count: int = (
        await session.execute(
            select(func.count())
            .select_from(Job)
            .where(Job.user_id == user_id)
            .where(Job.active.is_(True))
            .where(Job.cancelled_at.is_(None))
        )
    ).scalar_one()
    if total_count >= limits.active_total_per_user:
        return Reject(reason="active_total_per_user", retry_after_seconds=_RETRY_AFTER)

    # 3. Global active recurring ceiling
    global_recurring: int = (
        await session.execute(
            select(func.count())
            .select_from(Job)
            .where(Job.active.is_(True))
            .where(Job.job_type == "recurring")
            .where(Job.cancelled_at.is_(None))
        )
    ).scalar_one()
    if global_recurring >= limits.global_active_recurring:
        return Reject(reason="global_active_recurring", retry_after_seconds=_RETRY_AFTER)

    return Allow()
