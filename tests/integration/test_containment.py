"""Integration tests for containment caps (ADR-055).

Tests the three new caps (active-recurring per user, active-total per user,
global active-recurring ceiling) and OPERATOR_USER_ID exemption against a
live Postgres instance.

Run: uv run pytest -m integration tests/integration/test_containment.py
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.engine import create_async_engine
from app.mcp.server import _handle_task_create
from app.quotas.containment import Allow, ContainmentLimits, Reject, check_containment


@pytest_asyncio.fixture
async def session_factory():
    """Fresh engine per test — avoids cross-event-loop pool reuse."""
    engine = create_async_engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    async with factory() as session:
        async with session.begin():
            await session.execute(text("DELETE FROM run_events"))
            await session.execute(text("DELETE FROM job_runs"))
            await session.execute(text("DELETE FROM jobs"))
    await engine.dispose()


async def _bulk_insert_recurring(
    factory: async_sessionmaker,
    user_id: str,
    count: int,
    *,
    created_at_offset: str = "now()",
) -> None:
    """Insert *count* active recurring jobs for *user_id* (bypasses ORM defaults).

    *created_at_offset* is a Postgres expression for created_at/updated_at.
    Pass e.g. ``"now() - interval '2 hours'"`` to seed outside rate-limit windows.
    """
    async with factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "INSERT INTO jobs "
                    "(user_id, description, action, action_params, job_type, "
                    f"cron_expr, timezone, active, cancelled_at, created_at, updated_at) "
                    f"SELECT :user_id, 'containment-seed', 'echo', '{{}}', 'recurring', "
                    f"'* * * * *', 'UTC', true, NULL, {created_at_offset}, {created_at_offset} "
                    "FROM generate_series(1, :count)"
                ),
                {"user_id": user_id, "count": count},
            )


async def _bulk_insert_one_shot(
    factory: async_sessionmaker,
    user_id: str,
    count: int,
    *,
    created_at_offset: str = "now()",
) -> None:
    """Insert *count* active one-shot jobs for *user_id*.

    *created_at_offset* is a Postgres expression for created_at/updated_at.
    """
    async with factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "INSERT INTO jobs "
                    "(user_id, description, action, action_params, job_type, "
                    f"scheduled_at, timezone, active, cancelled_at, created_at, updated_at) "
                    f"SELECT :user_id, 'containment-seed', 'echo', '{{}}', 'one_shot', "
                    f"now() + interval '1 hour', 'UTC', true, NULL, "
                    f"{created_at_offset}, {created_at_offset} "
                    "FROM generate_series(1, :count)"
                ),
                {"user_id": user_id, "count": count},
            )


# ---------------------------------------------------------------------------
# Direct checker tests (bypass handler for speed)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_checker_allow_when_empty(session_factory):
    """No jobs seeded → checker returns Allow."""
    limits = ContainmentLimits(
        active_recurring_per_user=5,
        active_total_per_user=50,
        global_active_recurring=500,
    )
    async with session_factory() as session:
        decision = await check_containment("ct-user-allow", session, limits)
    assert isinstance(decision, Allow)


@pytest.mark.integration
async def test_checker_reject_active_recurring_per_user(session_factory):
    """5 active recurring jobs → next check returns Reject(active_recurring_per_user)."""
    user_id = "ct-user-recurring"
    await _bulk_insert_recurring(session_factory, user_id, 5)

    limits = ContainmentLimits(
        active_recurring_per_user=5,
        active_total_per_user=50,
        global_active_recurring=500,
    )
    async with session_factory() as session:
        decision = await check_containment(user_id, session, limits)

    assert isinstance(decision, Reject)
    assert decision.reason == "active_recurring_per_user"
    assert decision.retry_after_seconds > 0


@pytest.mark.integration
async def test_checker_reject_active_total_per_user(session_factory):
    """50 active jobs (one-shot) → next check returns Reject(active_total_per_user)."""
    user_id = "ct-user-total"
    await _bulk_insert_one_shot(session_factory, user_id, 50)

    limits = ContainmentLimits(
        active_recurring_per_user=5,
        active_total_per_user=50,
        global_active_recurring=500,
    )
    async with session_factory() as session:
        decision = await check_containment(user_id, session, limits)

    assert isinstance(decision, Reject)
    assert decision.reason == "active_total_per_user"
    assert decision.retry_after_seconds > 0


@pytest.mark.integration
async def test_checker_reject_global_active_recurring(session_factory):
    """500 active recurring jobs globally → Reject(global_active_recurring)."""
    await _bulk_insert_recurring(session_factory, "ct-global-seed", 500)

    limits = ContainmentLimits(
        active_recurring_per_user=5,
        active_total_per_user=50,
        global_active_recurring=500,
    )
    async with session_factory() as session:
        # A different user who hasn't hit their personal cap
        decision = await check_containment("ct-global-victim", session, limits)

    assert isinstance(decision, Reject)
    assert decision.reason == "global_active_recurring"
    assert decision.retry_after_seconds > 0


@pytest.mark.integration
async def test_checker_cancelled_jobs_not_counted(session_factory):
    """Cancelled recurring jobs don't count towards the active recurring cap."""
    user_id = "ct-user-cancelled"
    # Insert 5 recurring jobs, then cancel them all
    await _bulk_insert_recurring(session_factory, user_id, 5)
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text("UPDATE jobs SET cancelled_at = now() WHERE user_id = :user_id"),
                {"user_id": user_id},
            )

    limits = ContainmentLimits(
        active_recurring_per_user=5,
        active_total_per_user=50,
        global_active_recurring=500,
    )
    async with session_factory() as session:
        decision = await check_containment(user_id, session, limits)

    assert isinstance(decision, Allow)


@pytest.mark.integration
async def test_checker_multi_user_isolation(session_factory):
    """User A at recurring limit doesn't affect user B."""
    await _bulk_insert_recurring(session_factory, "ct-iso-user-a", 5)

    limits = ContainmentLimits(
        active_recurring_per_user=5,
        active_total_per_user=50,
        global_active_recurring=500,
    )

    async with session_factory() as session:
        decision_a = await check_containment("ct-iso-user-a", session, limits)
    assert isinstance(decision_a, Reject)
    assert decision_a.reason == "active_recurring_per_user"

    async with session_factory() as session:
        decision_b = await check_containment("ct-iso-user-b", session, limits)
    assert isinstance(decision_b, Allow)


@pytest.mark.integration
async def test_checker_operator_bypasses_recurring_cap(session_factory):
    """Operator with is_operator=True is not checked even at cap."""
    user_id = "ct-operator"
    await _bulk_insert_recurring(session_factory, user_id, 5)

    limits = ContainmentLimits(
        active_recurring_per_user=5,
        active_total_per_user=50,
        global_active_recurring=500,
    )
    async with session_factory() as session:
        decision = await check_containment(user_id, session, limits, is_operator=True)

    assert isinstance(decision, Allow)


# ---------------------------------------------------------------------------
# Handler-level tests (task.create.v1 end-to-end)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_handler_recurring_cap_rejected(session_factory):
    """5 recurring jobs → 6th task.create.v1 returns USER_INPUT quota exceeded."""
    user_id = "ct-handler-recurring"
    # Seed outside rate-limit windows (>24h daily, >1min burst) so rate-limit
    # passes first and the containment cap is the one that fires.
    await _bulk_insert_recurring(
        session_factory, user_id, 5, created_at_offset="now() - interval '25 hours'"
    )

    result = await _handle_task_create(
        arguments={
            "action": "echo",
            "action_params": {"message": "one more"},
            "schedule_type": "recurring",
            "cron_expr": "0 8 * * *",
        },
        user_id=user_id,
        session_factory=session_factory,
    )

    assert result["ok"] is False
    err = result["error"]
    assert err["code"] == "USER_INPUT"
    assert "active_recurring_per_user" in err["message"]
    assert err["field"] == "user_id"
    assert isinstance(err["expected"], dict)
    assert err["expected"]["retry_after_seconds"] > 0


@pytest.mark.integration
async def test_handler_total_cap_rejected(session_factory):
    """50 active jobs → next task.create.v1 returns USER_INPUT quota exceeded."""
    user_id = "ct-handler-total"
    # Seed outside rate-limit windows so the containment cap is what fires.
    await _bulk_insert_one_shot(
        session_factory, user_id, 50, created_at_offset="now() - interval '25 hours'"
    )

    result = await _handle_task_create(
        arguments={"action": "echo", "action_params": {"message": "too many"}},
        user_id=user_id,
        session_factory=session_factory,
    )

    assert result["ok"] is False
    err = result["error"]
    assert err["code"] == "USER_INPUT"
    assert "active_total_per_user" in err["message"]
    assert err["field"] == "user_id"
    assert isinstance(err["expected"], dict)
    assert err["expected"]["retry_after_seconds"] > 0


@pytest.mark.integration
async def test_handler_global_recurring_ceiling_rejected(session_factory):
    """500 global recurring jobs → task.create.v1 from another user is rejected."""
    await _bulk_insert_recurring(session_factory, "ct-global-flood", 500)

    result = await _handle_task_create(
        arguments={
            "action": "echo",
            "action_params": {"message": "global full"},
            "schedule_type": "recurring",
            "cron_expr": "0 8 * * *",
        },
        user_id="ct-global-victim-handler",
        session_factory=session_factory,
    )

    assert result["ok"] is False
    err = result["error"]
    assert err["code"] == "USER_INPUT"
    assert "global_active_recurring" in err["message"]


@pytest.mark.integration
async def test_handler_operator_bypasses_all_caps(session_factory, monkeypatch):
    """OPERATOR_USER_ID is exempt from all containment caps."""
    import app.mcp.server as _server_mod

    operator_id = "ct-operator-user"
    # Patch the settings singleton referenced by the server module directly,
    # since the module-level `settings` object is bound at import time.
    monkeypatch.setattr(_server_mod.settings, "operator_user_id", operator_id)

    # Seed at the recurring cap for the operator (outside rate-limit windows)
    await _bulk_insert_recurring(
        session_factory, operator_id, 5, created_at_offset="now() - interval '25 hours'"
    )

    result = await _handle_task_create(
        arguments={
            "action": "echo",
            "action_params": {"message": "operator override"},
            "schedule_type": "recurring",
            "cron_expr": "0 8 * * *",
        },
        user_id=operator_id,
        session_factory=session_factory,
    )

    assert result["ok"] is True
    assert "job_id" in result["data"]


@pytest.mark.integration
async def test_handler_under_all_caps_succeeds(session_factory):
    """Handler succeeds when user is under all containment caps."""
    result = await _handle_task_create(
        arguments={"action": "echo", "action_params": {"message": "ok"}},
        user_id="ct-handler-ok",
        session_factory=session_factory,
    )

    assert result["ok"] is True
    assert "job_id" in result["data"]
