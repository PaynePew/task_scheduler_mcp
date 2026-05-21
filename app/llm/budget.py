"""Token budget accounting for operator-subsidized LLM actions (ADR-052).

Two caps enforced here:
  1. Per-user daily token budget   — resets at UTC midnight (budget_date rolls over).
  2. Global monthly token ceiling  — sum of all users' tokens this calendar month.

Caller flow:
  1. Call ``check_token_budget`` before the LLM call; if not Allow, return error.
  2. Make the LLM call.
  3. Call ``record_token_usage`` with the tokens actually consumed.

``check_token_budget`` does NOT reserve tokens — there is a small TOCTOU window
if many calls land simultaneously near the budget boundary. This is acceptable
per ADR-052: the provider dashboard hard stop is the absolute last-resort; the
app-side counters are a best-effort cost guardrail, not a hard financial control.

Pattern mirrors ``app/ratelimit/checker.py`` (ADR-042) and
``app/quotas/containment.py`` (ADR-055).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import LlmTokenBudget


@dataclass(frozen=True)
class TokenBudgetLimits:
    daily_per_user: int
    global_monthly: int | None  # None = disabled (provider dashboard only)


@dataclass(frozen=True)
class Allow:
    pass


@dataclass(frozen=True)
class DailyBudgetExceeded:
    """Per-user daily cap exhausted; retry after UTC midnight."""

    retry_after_seconds: int


@dataclass(frozen=True)
class GlobalCeilingExceeded:
    """Global monthly token ceiling tripped; retry after first of next month (UTC)."""

    retry_after_seconds: int


BudgetDecision = Allow | DailyBudgetExceeded | GlobalCeilingExceeded


def _seconds_until_utc_midnight(now: datetime) -> int:
    """Seconds from *now* until 00:00:00 UTC the next day."""
    tomorrow = now.date() + timedelta(days=1)
    midnight = datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=UTC)
    return max(1, math.ceil((midnight - now).total_seconds()))


def _seconds_until_next_month(now: datetime) -> int:
    """Seconds from *now* until 00:00:00 UTC on the first day of the next month."""
    year, month = now.year, now.month
    if month == 12:
        year, month = year + 1, 1
    else:
        month += 1
    first_of_next = datetime(year, month, 1, tzinfo=UTC)
    return max(1, math.ceil((first_of_next - now).total_seconds()))


async def check_token_budget(
    user_id: str,
    session: AsyncSession,
    limits: TokenBudgetLimits,
    *,
    _now: datetime | None = None,
) -> BudgetDecision:
    """Return Allow or a rejection variant for *user_id*.

    Checks global monthly ceiling first (cheaper global early-exit), then the
    per-user daily budget.

    *_now* is injectable for unit-test determinism; production leaves it None.
    """
    now = _now if _now is not None else datetime.now(UTC)
    today: date = now.date()

    # --- global monthly ceiling ---
    if limits.global_monthly is not None:
        # Sum tokens_used across ALL users for the current calendar month.
        month_start = date(today.year, today.month, 1)
        global_total: int = (
            await session.execute(
                select(func.coalesce(func.sum(LlmTokenBudget.tokens_used), 0)).where(
                    LlmTokenBudget.budget_date >= month_start
                )
            )
        ).scalar_one()
        if global_total >= limits.global_monthly:
            return GlobalCeilingExceeded(retry_after_seconds=_seconds_until_next_month(now))

    # --- per-user daily budget ---
    row = (
        await session.execute(
            select(LlmTokenBudget.tokens_used).where(
                LlmTokenBudget.user_id == user_id,
                LlmTokenBudget.budget_date == today,
            )
        )
    ).scalar_one_or_none()
    user_today = row if row is not None else 0
    if user_today >= limits.daily_per_user:
        return DailyBudgetExceeded(retry_after_seconds=_seconds_until_utc_midnight(now))

    return Allow()


async def record_token_usage(
    user_id: str,
    session: AsyncSession,
    tokens_used: int,
    *,
    _now: datetime | None = None,
) -> None:
    """Upsert token usage into llm_token_budgets for today's row.

    Uses an INSERT … ON CONFLICT DO UPDATE (Postgres upsert) so concurrent
    calls accumulate correctly without a prior SELECT.
    """
    now = _now if _now is not None else datetime.now(UTC)
    today: date = now.date()

    stmt = (
        pg_insert(LlmTokenBudget)
        .values(
            user_id=user_id,
            budget_date=today,
            tokens_used=tokens_used,
            updated_at=now,
        )
        .on_conflict_do_update(
            index_elements=["user_id", "budget_date"],
            set_={
                "tokens_used": LlmTokenBudget.tokens_used + tokens_used,
                "updated_at": now,
            },
        )
    )
    await session.execute(stmt)
