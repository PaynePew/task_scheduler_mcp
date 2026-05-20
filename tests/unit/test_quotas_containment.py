"""Unit tests for app/quotas/containment — all DB calls mocked."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from app.quotas.containment import Allow, ContainmentLimits, Reject, check_containment


def _make_session(*scalar_results: int) -> AsyncMock:
    """Return a mock AsyncSession whose execute().scalar_one() returns values in order."""
    results = list(scalar_results)

    async def _execute(stmt):
        value = results.pop(0)
        mock_result = MagicMock()
        mock_result.scalar_one.return_value = value
        return mock_result

    session = AsyncMock()
    session.execute = _execute
    return session


_LIMITS = ContainmentLimits(
    active_recurring_per_user=5,
    active_total_per_user=50,
    global_active_recurring=500,
)


# ---------------------------------------------------------------------------
# Allow cases
# ---------------------------------------------------------------------------


async def test_allow_when_all_under_limits():
    # recurring=2, total=10, global_recurring=100 — all under
    session = _make_session(2, 10, 100)
    decision = await check_containment("u1", session, _LIMITS)
    assert isinstance(decision, Allow)


async def test_allow_at_one_under_recurring_limit():
    session = _make_session(4, 4, 0)
    decision = await check_containment("u1", session, _LIMITS)
    assert isinstance(decision, Allow)


async def test_allow_at_one_under_total_limit():
    session = _make_session(0, 49, 0)
    decision = await check_containment("u1", session, _LIMITS)
    assert isinstance(decision, Allow)


async def test_allow_at_one_under_global_limit():
    session = _make_session(0, 0, 499)
    decision = await check_containment("u1", session, _LIMITS)
    assert isinstance(decision, Allow)


# ---------------------------------------------------------------------------
# Active recurring per user cap
# ---------------------------------------------------------------------------


async def test_reject_at_recurring_limit():
    """count == limit → Reject."""
    session = _make_session(5, 0, 0)
    decision = await check_containment("u1", session, _LIMITS)
    assert isinstance(decision, Reject)
    assert decision.reason == "active_recurring_per_user"
    assert decision.retry_after_seconds > 0


async def test_reject_over_recurring_limit():
    session = _make_session(10, 0, 0)
    decision = await check_containment("u1", session, _LIMITS)
    assert isinstance(decision, Reject)
    assert decision.reason == "active_recurring_per_user"


async def test_recurring_check_exits_before_total_and_global():
    """When recurring limit is hit, the mock only needs one return value (early exit)."""
    session = _make_session(5)  # only one value — extra calls would IndexError
    decision = await check_containment("u1", session, _LIMITS)
    assert isinstance(decision, Reject)
    assert decision.reason == "active_recurring_per_user"


# ---------------------------------------------------------------------------
# Active total per user cap
# ---------------------------------------------------------------------------


async def test_reject_at_total_limit():
    """count == limit → Reject."""
    session = _make_session(0, 50, 0)
    decision = await check_containment("u1", session, _LIMITS)
    assert isinstance(decision, Reject)
    assert decision.reason == "active_total_per_user"
    assert decision.retry_after_seconds > 0


async def test_reject_over_total_limit():
    session = _make_session(0, 99, 0)
    decision = await check_containment("u1", session, _LIMITS)
    assert isinstance(decision, Reject)
    assert decision.reason == "active_total_per_user"


async def test_total_check_exits_before_global():
    """When total limit is hit, global query is not executed."""
    session = _make_session(0, 50)  # only two values — third call would IndexError
    decision = await check_containment("u1", session, _LIMITS)
    assert isinstance(decision, Reject)
    assert decision.reason == "active_total_per_user"


# ---------------------------------------------------------------------------
# Global active recurring ceiling
# ---------------------------------------------------------------------------


async def test_reject_at_global_recurring_limit():
    session = _make_session(0, 0, 500)
    decision = await check_containment("u1", session, _LIMITS)
    assert isinstance(decision, Reject)
    assert decision.reason == "global_active_recurring"
    assert decision.retry_after_seconds > 0


async def test_reject_over_global_recurring_limit():
    session = _make_session(0, 0, 999)
    decision = await check_containment("u1", session, _LIMITS)
    assert isinstance(decision, Reject)
    assert decision.reason == "global_active_recurring"


# ---------------------------------------------------------------------------
# Operator exemption
# ---------------------------------------------------------------------------


async def test_operator_bypasses_recurring_cap():
    """is_operator=True returns Allow even when recurring count exceeds limit."""
    session = _make_session()  # no DB calls expected
    decision = await check_containment("op", session, _LIMITS, is_operator=True)
    assert isinstance(decision, Allow)


async def test_operator_bypasses_total_cap():
    session = _make_session()
    decision = await check_containment("op", session, _LIMITS, is_operator=True)
    assert isinstance(decision, Allow)


async def test_operator_bypasses_global_cap():
    session = _make_session()
    decision = await check_containment("op", session, _LIMITS, is_operator=True)
    assert isinstance(decision, Allow)


async def test_non_operator_is_not_exempt():
    """is_operator=False (default) enforces the cap normally."""
    session = _make_session(5, 0, 0)
    decision = await check_containment("regular-user", session, _LIMITS)
    assert isinstance(decision, Reject)
    assert decision.reason == "active_recurring_per_user"


# ---------------------------------------------------------------------------
# Env-var override (settings level)
# ---------------------------------------------------------------------------


async def test_quota_env_var_overrides(monkeypatch):
    """Env vars propagate into Settings."""
    monkeypatch.setenv("QUOTA_ACTIVE_RECURRING_PER_USER", "2")
    monkeypatch.setenv("QUOTA_ACTIVE_TOTAL_PER_USER", "10")
    monkeypatch.setenv("QUOTA_GLOBAL_ACTIVE_RECURRING", "20")
    monkeypatch.setenv("OPERATOR_USER_ID", "sys-operator")

    from app.config.settings import Settings

    # pydantic-settings reads env on instantiation, so no module reload is needed.
    fresh = Settings()
    assert fresh.quota_active_recurring_per_user == 2
    assert fresh.quota_active_total_per_user == 10
    assert fresh.quota_global_active_recurring == 20
    assert fresh.operator_user_id == "sys-operator"


async def test_rate_limit_defaults_revised():
    """ADR-055 revised defaults: 100/day, 5/min."""
    from app.config.settings import Settings

    fresh = Settings()
    assert fresh.rate_limit_daily == 100
    assert fresh.rate_limit_burst_per_minute == 5
