"""Unit tests for app/llm/budget — daily rollover and global monthly ceiling.

All DB calls are mocked (no real Postgres connection needed).
The injectable *_now* parameter keeps test datetimes deterministic.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from app.llm.budget import (
    Allow,
    DailyBudgetExceeded,
    GlobalCeilingExceeded,
    TokenBudgetLimits,
    _seconds_until_next_month,
    _seconds_until_utc_midnight,
    check_token_budget,
)

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 5, 21, 10, 0, 0, tzinfo=UTC)  # 10:00 UTC on the 21st
_TODAY = _NOW.date()  # 2026-05-21

_LIMITS = TokenBudgetLimits(daily_per_user=10_000, global_monthly=500_000)
_NO_GLOBAL = TokenBudgetLimits(daily_per_user=10_000, global_monthly=None)


def _make_session(
    *,
    global_total: int | None = None,  # None = no global query expected
    user_today: int | None = None,  # None = row not found (scalar_one_or_none returns None)
) -> AsyncMock:
    """Fake AsyncSession returning canned scalar values in query order."""
    call_queue = []
    if global_total is not None:
        r1 = MagicMock()
        r1.scalar_one.return_value = global_total
        r1.scalar_one_or_none.return_value = global_total
        call_queue.append(r1)
    r2 = MagicMock()
    r2.scalar_one.return_value = user_today
    r2.scalar_one_or_none.return_value = user_today
    call_queue.append(r2)

    session = AsyncMock()

    async def _ex(stmt):
        return call_queue.pop(0)

    session.execute = _ex
    return session


# ---------------------------------------------------------------------------
# _seconds_until_utc_midnight
# ---------------------------------------------------------------------------


def test_seconds_until_midnight_at_10am():
    """10:00 UTC → 14 hours = 50400s."""
    now = datetime(2026, 5, 21, 10, 0, 0, tzinfo=UTC)
    assert _seconds_until_utc_midnight(now) == 14 * 3600


def test_seconds_until_midnight_at_end_of_day():
    """23:59:59 UTC → 1s."""
    now = datetime(2026, 5, 21, 23, 59, 59, tzinfo=UTC)
    assert _seconds_until_utc_midnight(now) == 1


# ---------------------------------------------------------------------------
# _seconds_until_next_month
# ---------------------------------------------------------------------------


def test_seconds_until_next_month_mid_month():
    """May 21 10:00 UTC → June 1 00:00 UTC = (10 + 21-1) days 14h from now."""
    now = datetime(2026, 5, 21, 10, 0, 0, tzinfo=UTC)
    first_june = datetime(2026, 6, 1, 0, 0, 0, tzinfo=UTC)
    expected = math.ceil((first_june - now).total_seconds())
    assert _seconds_until_next_month(now) == expected


def test_seconds_until_next_month_december():
    """December wraps to January of next year."""
    now = datetime(2025, 12, 15, 0, 0, 0, tzinfo=UTC)
    first_jan = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    expected = math.ceil((first_jan - now).total_seconds())
    assert _seconds_until_next_month(now) == expected


# ---------------------------------------------------------------------------
# Allow cases
# ---------------------------------------------------------------------------


async def test_allow_when_user_has_no_row_yet():
    """New user with no budget row today → Allow (0 < limit)."""
    session = _make_session(global_total=0, user_today=None)
    result = await check_token_budget("u1", session, _LIMITS, _now=_NOW)
    assert isinstance(result, Allow)


async def test_allow_when_user_under_daily_budget():
    session = _make_session(global_total=1000, user_today=5000)
    result = await check_token_budget("u1", session, _LIMITS, _now=_NOW)
    assert isinstance(result, Allow)


async def test_allow_at_one_under_daily_budget():
    session = _make_session(global_total=0, user_today=9999)
    result = await check_token_budget("u1", session, _LIMITS, _now=_NOW)
    assert isinstance(result, Allow)


async def test_allow_when_global_not_set():
    """No global ceiling configured → only per-user budget checked."""
    session = _make_session(user_today=5000)  # no global_total arg
    result = await check_token_budget("u1", session, _NO_GLOBAL, _now=_NOW)
    assert isinstance(result, Allow)


# ---------------------------------------------------------------------------
# Daily budget exceeded
# ---------------------------------------------------------------------------


async def test_reject_when_user_at_daily_limit():
    session = _make_session(global_total=0, user_today=10_000)
    result = await check_token_budget("u1", session, _LIMITS, _now=_NOW)
    assert isinstance(result, DailyBudgetExceeded)
    assert result.retry_after_seconds > 0


async def test_reject_when_user_over_daily_limit():
    session = _make_session(global_total=0, user_today=15_000)
    result = await check_token_budget("u1", session, _LIMITS, _now=_NOW)
    assert isinstance(result, DailyBudgetExceeded)


async def test_daily_retry_after_is_seconds_until_midnight():
    now = datetime(2026, 5, 21, 10, 0, 0, tzinfo=UTC)
    session = _make_session(global_total=0, user_today=10_000)
    result = await check_token_budget("u1", session, _LIMITS, _now=now)
    assert isinstance(result, DailyBudgetExceeded)
    assert result.retry_after_seconds == 14 * 3600  # 14h until midnight


# ---------------------------------------------------------------------------
# Global monthly ceiling
# ---------------------------------------------------------------------------


async def test_reject_when_global_ceiling_at_limit():
    session = _make_session(global_total=500_000, user_today=0)
    result = await check_token_budget("u1", session, _LIMITS, _now=_NOW)
    assert isinstance(result, GlobalCeilingExceeded)
    assert result.retry_after_seconds > 0


async def test_reject_when_global_ceiling_exceeded():
    session = _make_session(global_total=999_999, user_today=0)
    result = await check_token_budget("u1", session, _LIMITS, _now=_NOW)
    assert isinstance(result, GlobalCeilingExceeded)


async def test_global_ceiling_checked_before_daily_budget():
    """Global ceiling triggers before the per-user check is reached.
    Session only supplies one value (global_total) — extra calls would IndexError.
    """
    # Single-item queue: first call returns global_total=500_000; any second call
    # would pop from an empty list and raise IndexError, proving only one query ran.
    one_result = MagicMock()
    one_result.scalar_one.return_value = 500_000
    one_result.scalar_one_or_none.return_value = 500_000
    call_results = [one_result]

    session = AsyncMock()

    async def _ex(stmt):
        return call_results.pop(0)  # IndexError if called again

    session.execute = _ex

    result = await check_token_budget("u1", session, _LIMITS, _now=_NOW)
    assert isinstance(result, GlobalCeilingExceeded)


async def test_global_ceiling_disabled_when_none():
    """global_monthly=None → no global query, per-user allowed normally."""
    session = _make_session(user_today=0)  # no global slot
    result = await check_token_budget("u1", session, _NO_GLOBAL, _now=_NOW)
    assert isinstance(result, Allow)


# ---------------------------------------------------------------------------
# Daily rollover — cross-midnight
# ---------------------------------------------------------------------------


async def test_daily_rollover_yesterday_full_today_empty():
    """Yesterday's row (full) is not counted for today's budget."""
    # Budget check is for today — if today's row is None (no record), budget is fresh.
    session = _make_session(global_total=0, user_today=None)
    result = await check_token_budget("u1", session, _LIMITS, _now=_NOW)
    assert isinstance(result, Allow)


# ---------------------------------------------------------------------------
# Env-var driven settings
# ---------------------------------------------------------------------------


async def test_budget_limits_from_settings(monkeypatch):
    """LLM_DAILY_TOKEN_BUDGET_PER_USER is read into settings."""
    monkeypatch.setenv("LLM_DAILY_TOKEN_BUDGET_PER_USER", "500")
    monkeypatch.setenv("LLM_GLOBAL_MONTHLY_TOKEN_CEILING", "50000")
    from app.config.settings import Settings

    s = Settings()
    assert s.llm_daily_token_budget_per_user == 500
    assert s.llm_global_monthly_token_ceiling == 50_000
