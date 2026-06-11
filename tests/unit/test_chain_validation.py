"""Unit tests for app/domain/chain_validation.py.

Tests V1-V5 validation rules and the _is_match helper in chain_watcher.
CTE-based V4/V5 tests use mock sessions that return pre-canned CTE results.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.domain.chain_validation import (
    ChainCycleError,
    ChainDepthError,
    ChainJobNotFoundError,
    ChainJobTerminatedError,
    ChainRunSourceError,
    validate_chain,
    validate_run_source,
)
from app.workers.chain_watcher import _is_match

# ---------------------------------------------------------------------------
# _is_match helper — no DB required
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "trigger_on_status, event_type, expected",
    [
        # Literal match
        ("SUCCEEDED", "SUCCEEDED", True),
        ("FAILED", "FAILED", True),
        ("CANCELLED", "CANCELLED", True),
        # Literal mismatch
        ("SUCCEEDED", "FAILED", False),
        ("SUCCEEDED", "CANCELLED", False),
        ("FAILED", "SUCCEEDED", False),
        ("FAILED", "CANCELLED", False),
        ("CANCELLED", "SUCCEEDED", False),
        ("CANCELLED", "FAILED", False),
        # ANY matches everything including CANCELLED
        ("ANY", "SUCCEEDED", True),
        ("ANY", "FAILED", True),
        ("ANY", "CANCELLED", True),
        # None defaults to SUCCEEDED
        (None, "SUCCEEDED", True),
        (None, "FAILED", False),
        (None, "CANCELLED", False),
    ],
)
def test_is_match(trigger_on_status, event_type, expected):
    assert _is_match(trigger_on_status, event_type) == expected


# ---------------------------------------------------------------------------
# Helpers for building mock sessions
# ---------------------------------------------------------------------------


def _make_session_begin():
    @asynccontextmanager
    async def _begin():
        yield

    return _begin


def _mock_job(job_id=1, user_id="u1"):
    job = MagicMock()
    job.job_id = job_id
    job.user_id = user_id
    return job


def _mock_run(run_id=10, status="RUNNING"):
    run = MagicMock()
    run.run_id = run_id
    run.status = status
    return run


def _mock_cte_row(max_depth=1, has_cycle=False):
    row = MagicMock()
    row.max_depth = max_depth
    row.has_cycle = has_cycle
    return row


# ---------------------------------------------------------------------------
# V1: trigger job does not exist → ChainJobNotFoundError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_v1_trigger_job_not_found():
    job_result = MagicMock()
    job_result.scalar_one_or_none.return_value = None

    session = AsyncMock()
    session.execute = AsyncMock(return_value=job_result)

    with pytest.raises(ChainJobNotFoundError):
        await validate_chain(session, user_id="u1", trigger_on_job_id=999)


# ---------------------------------------------------------------------------
# V2: trigger job belongs to a different user → ChainJobNotFoundError (not 403)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_v2_cross_user_returns_not_found_not_403():
    trigger_job = _mock_job(job_id=1, user_id="other-user")

    job_result = MagicMock()
    job_result.scalar_one_or_none.return_value = trigger_job

    session = AsyncMock()
    session.execute = AsyncMock(return_value=job_result)

    with pytest.raises(ChainJobNotFoundError):
        await validate_chain(session, user_id="caller-user", trigger_on_job_id=1)


# ---------------------------------------------------------------------------
# V3: trigger job is fully terminated → ChainJobTerminatedError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_v3_trigger_job_fully_terminated_raises():
    trigger_job = _mock_job(user_id="u1")
    terminal_run = _mock_run(status="SUCCEEDED")

    job_result = MagicMock()
    job_result.scalar_one_or_none.return_value = trigger_job

    runs_result = MagicMock()
    runs_result.scalars.return_value.all.return_value = [terminal_run]

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[job_result, runs_result])

    with pytest.raises(ChainJobTerminatedError):
        await validate_chain(session, user_id="u1", trigger_on_job_id=1)


@pytest.mark.asyncio
async def test_v3_all_terminal_statuses_raise():
    """Each terminal status individually makes the job fully terminated."""
    for terminal in ("SUCCEEDED", "FAILED", "CANCELLED"):
        trigger_job = _mock_job(user_id="u1")
        terminal_run = _mock_run(status=terminal)

        job_result = MagicMock()
        job_result.scalar_one_or_none.return_value = trigger_job

        runs_result = MagicMock()
        runs_result.scalars.return_value.all.return_value = [terminal_run]

        session = AsyncMock()
        session.execute = AsyncMock(side_effect=[job_result, runs_result])

        with pytest.raises(ChainJobTerminatedError):
            await validate_chain(session, user_id="u1", trigger_on_job_id=1)


@pytest.mark.asyncio
async def test_v3_non_terminal_run_passes():
    """A RUNNING upstream run passes V3 and returns that run."""
    trigger_job = _mock_job(user_id="u1")
    active_run = _mock_run(status="RUNNING")

    job_result = MagicMock()
    job_result.scalar_one_or_none.return_value = trigger_job

    runs_result = MagicMock()
    runs_result.scalars.return_value.all.return_value = [active_run]

    # CTE result: depth=1, no cycle — passes V4/V5
    cte_result = MagicMock()
    cte_result.one.return_value = _mock_cte_row(max_depth=1, has_cycle=False)

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[job_result, runs_result, cte_result])

    result = await validate_chain(session, user_id="u1", trigger_on_job_id=1)
    assert result is active_run


# ---------------------------------------------------------------------------
# V4: cycle detected → ChainCycleError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_v4_cycle_detected_raises():
    trigger_job = _mock_job(user_id="u1")
    active_run = _mock_run(status="RUNNING")

    job_result = MagicMock()
    job_result.scalar_one_or_none.return_value = trigger_job

    runs_result = MagicMock()
    runs_result.scalars.return_value.all.return_value = [active_run]

    # CTE signals a cycle
    cte_result = MagicMock()
    cte_result.one.return_value = _mock_cte_row(max_depth=3, has_cycle=True)

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[job_result, runs_result, cte_result])

    with pytest.raises(ChainCycleError):
        await validate_chain(session, user_id="u1", trigger_on_job_id=1)


# ---------------------------------------------------------------------------
# V5: chain depth > 10 → ChainDepthError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_v5_depth_exactly_10_raises():
    """If the trigger job's ancestor chain is already 10 deep, adding 1 more exceeds the limit."""
    trigger_job = _mock_job(user_id="u1")
    active_run = _mock_run(status="RUNNING")

    job_result = MagicMock()
    job_result.scalar_one_or_none.return_value = trigger_job

    runs_result = MagicMock()
    runs_result.scalars.return_value.all.return_value = [active_run]

    # CTE returns max_depth=10, no cycle → adding new job makes 11
    cte_result = MagicMock()
    cte_result.one.return_value = _mock_cte_row(max_depth=10, has_cycle=False)

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[job_result, runs_result, cte_result])

    with pytest.raises(ChainDepthError):
        await validate_chain(session, user_id="u1", trigger_on_job_id=1)


@pytest.mark.asyncio
async def test_v5_depth_9_passes():
    """Ancestor chain of depth 9 allows one more job (total = 10 ≤ limit)."""
    trigger_job = _mock_job(user_id="u1")
    active_run = _mock_run(status="RUNNING")

    job_result = MagicMock()
    job_result.scalar_one_or_none.return_value = trigger_job

    runs_result = MagicMock()
    runs_result.scalars.return_value.all.return_value = [active_run]

    # CTE returns max_depth=9 — total with new job = 10, which is ≤ 10
    cte_result = MagicMock()
    cte_result.one.return_value = _mock_cte_row(max_depth=9, has_cycle=False)

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[job_result, runs_result, cte_result])

    result = await validate_chain(session, user_id="u1", trigger_on_job_id=1)
    assert result is active_run


@pytest.mark.asyncio
async def test_v5_depth_11_raises():
    """Ancestor chain exceeding 10 is rejected."""
    trigger_job = _mock_job(user_id="u1")
    active_run = _mock_run(status="RUNNING")

    job_result = MagicMock()
    job_result.scalar_one_or_none.return_value = trigger_job

    runs_result = MagicMock()
    runs_result.scalars.return_value.all.return_value = [active_run]

    cte_result = MagicMock()
    cte_result.one.return_value = _mock_cte_row(max_depth=11, has_cycle=False)

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[job_result, runs_result, cte_result])

    with pytest.raises(ChainDepthError):
        await validate_chain(session, user_id="u1", trigger_on_job_id=1)


# ---------------------------------------------------------------------------
# Error codes / fields via map_domain_error
# ---------------------------------------------------------------------------


def test_chain_error_codes_via_map_domain_error():
    """map_domain_error produces correct code / field / expected for each chain error."""
    from app.mcp.errors import map_domain_error

    cases = [
        (ChainJobNotFoundError(99), "NOT_FOUND", "trigger_on_job_id", None),
        (ChainJobTerminatedError(99), "INVALID_STATE", "trigger_on_job_id", None),
        (ChainCycleError(99), "USER_INPUT", "trigger_on_job_id", "non-circular chain"),
        (ChainDepthError(99), "USER_INPUT", "trigger_on_job_id", "chain depth ≤ 10"),
        (ChainRunSourceError(), "USER_INPUT", "trigger_on_job_id", None),
    ]

    for exc, expected_code, expected_field, expected_expected in cases:
        result = map_domain_error(exc)
        assert result["ok"] is False, f"{type(exc).__name__} should return ok=False"
        assert result["error"]["code"] == expected_code, f"{type(exc).__name__}: code mismatch"
        assert result["error"]["field"] == expected_field, f"{type(exc).__name__}: field mismatch"
        if expected_expected is not None:
            assert result["error"]["expected"] == expected_expected, (
                f"{type(exc).__name__}: expected 'expected' {expected_expected!r}, "
                f"got {result['error'].get('expected')!r}"
            )


# ---------------------------------------------------------------------------
# V6: both trigger_on_job_id and cron_expr set → ChainRunSourceError
# ---------------------------------------------------------------------------


def test_v6_both_set_raises():
    """A job with both trigger_on_job_id and cron_expr is rejected (V6)."""
    with pytest.raises(ChainRunSourceError):
        validate_run_source(trigger_on_job_id=5, cron_expr="0 8 * * *")


def test_v6_only_cron_passes():
    """A schedule-driven job (cron only, no trigger) passes V6."""
    # Must not raise
    validate_run_source(trigger_on_job_id=None, cron_expr="0 8 * * *")


def test_v6_only_trigger_passes():
    """A trigger-driven (chained) job with no cron passes V6."""
    # Must not raise
    validate_run_source(trigger_on_job_id=5, cron_expr=None)


def test_v6_neither_set_passes():
    """An immediate/one-shot job with neither cron nor trigger passes V6."""
    # Must not raise
    validate_run_source(trigger_on_job_id=None, cron_expr=None)


def test_v6_error_maps_to_user_input():
    """map_domain_error maps ChainRunSourceError to USER_INPUT on trigger_on_job_id."""
    from app.mcp.errors import map_domain_error

    result = map_domain_error(ChainRunSourceError())
    assert result["ok"] is False
    assert result["error"]["code"] == "USER_INPUT"
    assert result["error"]["field"] == "trigger_on_job_id"
