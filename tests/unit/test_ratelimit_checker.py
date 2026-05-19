"""Unit tests for app/ratelimit/checker — all DB calls mocked."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.ratelimit.checker import Allow, RateLimits, Reject, check_rate_limit


def _make_session(burst_result: tuple, daily_result: tuple) -> AsyncMock:
    """Return a mock AsyncSession that returns *burst_result* then *daily_result*."""
    call_results = [burst_result, daily_result]

    async def _execute(stmt):
        row = call_results.pop(0)
        mock_result = MagicMock()
        mock_result.one.return_value = row
        return mock_result

    session = AsyncMock()
    session.execute = _execute
    return session


_NOW = datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC)
_BURST_OLDEST = _NOW - timedelta(seconds=30)  # 30s ago — inside 1-min burst window
_DAILY_OLDEST = _NOW - timedelta(hours=12)  # 12h ago — inside 24h daily window


# ---------------------------------------------------------------------------
# Allow cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_allow_when_under_both_limits():
    limits = RateLimits(daily=1000, burst=10)
    session = _make_session((5, _BURST_OLDEST), (100, _DAILY_OLDEST))
    decision = await check_rate_limit("u1", session, limits, _now=_NOW)
    assert isinstance(decision, Allow)


@pytest.mark.asyncio
async def test_allow_at_one_under_burst_limit():
    limits = RateLimits(daily=1000, burst=10)
    session = _make_session((9, _BURST_OLDEST), (9, _DAILY_OLDEST))
    decision = await check_rate_limit("u1", session, limits, _now=_NOW)
    assert isinstance(decision, Allow)


@pytest.mark.asyncio
async def test_allow_at_one_under_daily_limit():
    limits = RateLimits(daily=1000, burst=10)
    session = _make_session((0, None), (999, _DAILY_OLDEST))
    decision = await check_rate_limit("u1", session, limits, _now=_NOW)
    assert isinstance(decision, Allow)


# ---------------------------------------------------------------------------
# Burst reject cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reject_at_burst_limit():
    """count == burst limit → Reject."""
    limits = RateLimits(daily=1000, burst=10)
    session = _make_session((10, _BURST_OLDEST), (10, _DAILY_OLDEST))
    decision = await check_rate_limit("u1", session, limits, _now=_NOW)
    assert isinstance(decision, Reject)
    assert decision.reason == "burst"


@pytest.mark.asyncio
async def test_reject_over_burst_limit():
    limits = RateLimits(daily=1000, burst=10)
    session = _make_session((15, _BURST_OLDEST), (15, _DAILY_OLDEST))
    decision = await check_rate_limit("u1", session, limits, _now=_NOW)
    assert isinstance(decision, Reject)
    assert decision.reason == "burst"


@pytest.mark.asyncio
async def test_burst_retry_after_seconds_correct():
    """retry_after = ceil(oldest + 60s - now)."""
    limits = RateLimits(daily=1000, burst=10)
    oldest = _NOW - timedelta(seconds=30)  # ages out in 30s
    session = _make_session((10, oldest), (10, _DAILY_OLDEST))
    decision = await check_rate_limit("u1", session, limits, _now=_NOW)
    assert isinstance(decision, Reject)
    assert decision.retry_after_seconds == 30  # 60 - 30 = 30


@pytest.mark.asyncio
async def test_burst_retry_after_minimum_one():
    """retry_after is at least 1 even if the window has just expired."""
    limits = RateLimits(daily=1000, burst=10)
    oldest = _NOW - timedelta(seconds=59, milliseconds=999)  # ages out in ~0ms
    session = _make_session((10, oldest), (10, _DAILY_OLDEST))
    decision = await check_rate_limit("u1", session, limits, _now=_NOW)
    assert isinstance(decision, Reject)
    assert decision.retry_after_seconds >= 1


# ---------------------------------------------------------------------------
# Daily reject cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reject_at_daily_limit():
    """count == daily limit → Reject daily."""
    limits = RateLimits(daily=1000, burst=10)
    session = _make_session((0, None), (1000, _DAILY_OLDEST))
    decision = await check_rate_limit("u1", session, limits, _now=_NOW)
    assert isinstance(decision, Reject)
    assert decision.reason == "daily"


@pytest.mark.asyncio
async def test_daily_retry_after_seconds_correct():
    """retry_after = ceil(oldest + 24h - now)."""
    limits = RateLimits(daily=1000, burst=10)
    oldest = _NOW - timedelta(hours=23)  # ages out in 1h = 3600s
    session = _make_session((0, None), (1000, oldest))
    decision = await check_rate_limit("u1", session, limits, _now=_NOW)
    assert isinstance(decision, Reject)
    assert decision.retry_after_seconds == 3600


# ---------------------------------------------------------------------------
# Burst checked before daily
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_burst_checked_before_daily():
    """When both limits are exceeded, burst is returned (checked first)."""
    limits = RateLimits(daily=1000, burst=10)
    session = _make_session((10, _BURST_OLDEST), (1000, _DAILY_OLDEST))
    decision = await check_rate_limit("u1", session, limits, _now=_NOW)
    assert isinstance(decision, Reject)
    assert decision.reason == "burst"


# ---------------------------------------------------------------------------
# Window boundary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_window_boundary_burst_under_via_injected_now():
    """Simulating time 61s later makes the 'oldest' job fall outside the window.

    Because the checker queries the DB with the injected now, our mock returns
    (0, None) when we pretend now is 61s later — the burst window is empty.
    """
    limits = RateLimits(daily=1000, burst=10)
    later_now = _NOW + timedelta(seconds=61)
    # At later_now, the 1-min burst window starts at _NOW + 1s, so jobs created
    # at _BURST_OLDEST (= _NOW - 30s) are outside it → mock returns (0, None).
    session = _make_session((0, None), (0, None))
    decision = await check_rate_limit("u1", session, limits, _now=later_now)
    assert isinstance(decision, Allow)


# ---------------------------------------------------------------------------
# Multi-user isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multiple_users_isolated():
    """User A at daily limit does not block user B."""
    limits = RateLimits(daily=1000, burst=10)

    # User A: daily count = 1000 → Reject
    session_a = _make_session((0, None), (1000, _DAILY_OLDEST))
    decision_a = await check_rate_limit("user-A", session_a, limits, _now=_NOW)
    assert isinstance(decision_a, Reject)
    assert decision_a.reason == "daily"

    # User B: counts under limits → Allow
    session_b = _make_session((0, None), (5, _DAILY_OLDEST))
    decision_b = await check_rate_limit("user-B", session_b, limits, _now=_NOW)
    assert isinstance(decision_b, Allow)


# ---------------------------------------------------------------------------
# Env var override
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_limit_daily_env_var_override(monkeypatch):
    """RATE_LIMIT_DAILY=5 reduces the daily cap; Settings picks it up."""
    monkeypatch.setenv("RATE_LIMIT_DAILY", "5")

    # Re-import settings after patching env
    import importlib

    import app.config.settings as _settings_mod

    importlib.reload(_settings_mod)
    from app.config.settings import Settings

    fresh = Settings()
    assert fresh.rate_limit_daily == 5

    # Restore module state
    importlib.reload(_settings_mod)


@pytest.mark.asyncio
async def test_rate_limit_burst_env_var_override(monkeypatch):
    """RATE_LIMIT_BURST_PER_MINUTE=3 reduces the burst cap."""
    monkeypatch.setenv("RATE_LIMIT_BURST_PER_MINUTE", "3")

    import importlib

    import app.config.settings as _settings_mod

    importlib.reload(_settings_mod)
    from app.config.settings import Settings

    fresh = Settings()
    assert fresh.rate_limit_burst_per_minute == 3

    importlib.reload(_settings_mod)


# ---------------------------------------------------------------------------
# Limits reset after window elapses (unit-level simulation via _now injection)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_limits_reset_after_daily_window():
    """25h after seeding 1000 jobs, the daily window is empty → Allow."""
    limits = RateLimits(daily=1000, burst=10)
    # At _NOW + 25h the daily window starts 1h after the jobs were created.
    # The mock returns (0, None) — no jobs in that new window.
    later_now = _NOW + timedelta(hours=25)
    session = _make_session((0, None), (0, None))
    decision = await check_rate_limit("u1", session, limits, _now=later_now)
    assert isinstance(decision, Allow)


@pytest.mark.asyncio
async def test_limits_reset_after_burst_window():
    """2 minutes after a burst of 10, the burst window is empty → Allow."""
    limits = RateLimits(daily=1000, burst=10)
    later_now = _NOW + timedelta(minutes=2)
    session = _make_session((0, None), (10, _DAILY_OLDEST))
    decision = await check_rate_limit("u1", session, limits, _now=later_now)
    assert isinstance(decision, Allow)
