"""Unit tests for _flip_waiting_run decision order in chain_watcher (issue #236).

The decision tree must evaluate trigger-status match BEFORE the slow-consumer
check:
  1. Mismatch (regardless of busy state) → CANCELLED_BY_CHAIN_MISS
  2. Match + busy downstream           → CANCELLED_SLOW_CONSUMER
  3. Match + idle downstream           → PENDING / QUEUED_BY_CHAIN

The mismatch + busy combination (AC from #236) is the case that was mislabelled
CANCELLED_SLOW_CONSUMER on current main.

Run with:
    uv run pytest -m "not integration" tests/unit/test_chain_watcher_flip.py
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.db.models import Job, JobRun, RunEvent
from app.workers.chain_watcher import _flip_waiting_run

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_waiting_run(run_id: int = 1, job_id: int = 10) -> JobRun:
    run = MagicMock(spec=JobRun)
    run.run_id = run_id
    run.job_id = job_id
    run.time_bucket = "2026-06-12T00:00:00+00:00"
    run.status = "WAITING"
    return run


def _make_trigger_job(job_id: int = 10, trigger_on_status: str = "SUCCEEDED") -> Job:
    job = MagicMock(spec=Job)
    job.job_id = job_id
    job.trigger_on_status = trigger_on_status
    return job


def _make_session() -> AsyncMock:
    """Minimal async session mock — execute() and add() are tracked."""
    session = AsyncMock()
    session._added: list = []
    session.add = MagicMock(side_effect=lambda obj: session._added.append(obj))
    session.execute = AsyncMock(return_value=MagicMock())
    return session


def _emitted_event(session: AsyncMock) -> RunEvent | None:
    """Return the first RunEvent added to the session, or None."""
    for obj in session._added:
        if isinstance(obj, RunEvent):
            return obj
    return None


# ---------------------------------------------------------------------------
# Acceptance criteria from #236
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mismatch_and_busy_downstream_emits_chain_miss_not_slow_consumer():
    """Mismatch + busy downstream → CANCELLED_BY_CHAIN_MISS (AC #236, the mislabelled case).

    This is the regression: on current main the slow-consumer check runs first,
    so a mismatch while the downstream is busy gets the wrong audit label.
    After the fix, chain-miss verdict takes precedence.
    """
    waiting_run = _make_waiting_run(run_id=5, job_id=10)
    trigger_job = _make_trigger_job(job_id=10, trigger_on_status="SUCCEEDED")
    session = _make_session()
    now = datetime.now(tz=UTC)

    # Upstream event is FAILED → mismatch (trigger_on_status="SUCCEEDED")
    # Downstream IS busy (has_executing_run returns True)
    with patch("app.workers.chain_watcher.has_executing_run", new=AsyncMock(return_value=True)):
        await _flip_waiting_run(
            session,
            waiting_run=waiting_run,
            trigger_job=trigger_job,
            event_type="FAILED",  # mismatch against trigger_on_status="SUCCEEDED"
            now=now,
        )

    event = _emitted_event(session)
    assert event is not None, "A RunEvent must be emitted"
    assert event.event_type == "CANCELLED_BY_CHAIN_MISS", (
        f"Expected CANCELLED_BY_CHAIN_MISS but got {event.event_type!r}; "
        "chain-miss verdict must take precedence over slow-consumer drop"
    )
    assert event.status_to == "CANCELLED"


@pytest.mark.asyncio
async def test_match_and_busy_downstream_emits_slow_consumer():
    """Match + busy downstream → CANCELLED_SLOW_CONSUMER (AC #236)."""
    waiting_run = _make_waiting_run(run_id=6, job_id=11)
    trigger_job = _make_trigger_job(job_id=11, trigger_on_status="SUCCEEDED")
    session = _make_session()
    now = datetime.now(tz=UTC)

    # Upstream event is SUCCEEDED → match; downstream IS busy
    with patch("app.workers.chain_watcher.has_executing_run", new=AsyncMock(return_value=True)):
        await _flip_waiting_run(
            session,
            waiting_run=waiting_run,
            trigger_job=trigger_job,
            event_type="SUCCEEDED",  # match
            now=now,
        )

    event = _emitted_event(session)
    assert event is not None
    assert event.event_type == "CANCELLED_SLOW_CONSUMER", (
        f"Expected CANCELLED_SLOW_CONSUMER but got {event.event_type!r}"
    )
    assert event.status_to == "CANCELLED"


@pytest.mark.asyncio
async def test_match_and_idle_downstream_emits_queued_by_chain():
    """Match + idle downstream → PENDING / QUEUED_BY_CHAIN (AC #236, unchanged)."""
    waiting_run = _make_waiting_run(run_id=7, job_id=12)
    trigger_job = _make_trigger_job(job_id=12, trigger_on_status="SUCCEEDED")
    session = _make_session()
    now = datetime.now(tz=UTC)

    with patch("app.workers.chain_watcher.has_executing_run", new=AsyncMock(return_value=False)):
        await _flip_waiting_run(
            session,
            waiting_run=waiting_run,
            trigger_job=trigger_job,
            event_type="SUCCEEDED",  # match
            now=now,
        )

    event = _emitted_event(session)
    assert event is not None
    assert event.event_type == "QUEUED_BY_CHAIN", (
        f"Expected QUEUED_BY_CHAIN but got {event.event_type!r}"
    )
    assert event.status_to == "PENDING"


@pytest.mark.asyncio
async def test_mismatch_and_idle_downstream_emits_chain_miss():
    """Mismatch + idle downstream → CANCELLED_BY_CHAIN_MISS (unchanged baseline)."""
    waiting_run = _make_waiting_run(run_id=8, job_id=13)
    trigger_job = _make_trigger_job(job_id=13, trigger_on_status="SUCCEEDED")
    session = _make_session()
    now = datetime.now(tz=UTC)

    with patch("app.workers.chain_watcher.has_executing_run", new=AsyncMock(return_value=False)):
        await _flip_waiting_run(
            session,
            waiting_run=waiting_run,
            trigger_job=trigger_job,
            event_type="FAILED",  # mismatch
            now=now,
        )

    event = _emitted_event(session)
    assert event is not None
    assert event.event_type == "CANCELLED_BY_CHAIN_MISS"
    assert event.status_to == "CANCELLED"


@pytest.mark.asyncio
async def test_slow_consumer_check_not_called_on_mismatch():
    """has_executing_run must NOT be called when the event is a mismatch.

    Calling it on a mismatch is unnecessary work: the outcome is CANCELLED
    regardless of whether the downstream is busy.  After the fix, the
    short-circuit on mismatch skips the DB predicate entirely.
    """
    waiting_run = _make_waiting_run(run_id=9, job_id=14)
    trigger_job = _make_trigger_job(job_id=14, trigger_on_status="SUCCEEDED")
    session = _make_session()
    now = datetime.now(tz=UTC)

    with patch(
        "app.workers.chain_watcher.has_executing_run", new=AsyncMock(return_value=True)
    ) as mock_predicate:
        await _flip_waiting_run(
            session,
            waiting_run=waiting_run,
            trigger_job=trigger_job,
            event_type="FAILED",  # mismatch — predicate must not be called
            now=now,
        )

    mock_predicate.assert_not_called()
