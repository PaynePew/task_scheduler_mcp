"""Unit tests for app/workers/watcher.py — uses async mocks, no DB or SQS required."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.workers.watcher import LOOKAHEAD, claim_and_publish


def _async_cm(yield_value=None) -> MagicMock:
    """Create a MagicMock that acts as an async context manager yielding yield_value."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=yield_value)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _make_run(run_id: int, job_id: int, *, delta_seconds: int = -60) -> MagicMock:
    run = MagicMock()
    run.run_id = run_id
    run.job_id = job_id
    run.scheduled_at = datetime.now(tz=UTC) + timedelta(seconds=delta_seconds)
    return run


def _make_factory(runs: list) -> tuple[MagicMock, MagicMock]:
    """Return (factory, session_mock) configured to return the given runs from execute()."""
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = runs

    session = MagicMock()
    session.execute = AsyncMock(return_value=mock_result)
    session.add = MagicMock()
    # session.begin() is NOT a coroutine — it returns an async context manager directly
    session.begin = MagicMock(return_value=_async_cm(None))

    factory = MagicMock(return_value=_async_cm(session))
    return factory, session


@pytest.fixture
def mock_sqs() -> MagicMock:
    sqs = MagicMock()
    sqs.send_message_batch.return_value = {"Successful": [], "Failed": []}
    return sqs


@pytest.mark.asyncio
async def test_claim_and_publish_returns_count(mock_sqs):
    factory, _ = _make_factory([_make_run(42, 7)])
    assert await claim_and_publish(factory, mock_sqs) == 1


@pytest.mark.asyncio
async def test_claim_and_publish_sends_to_sqs(mock_sqs):
    run = _make_run(42, 7)
    factory, _ = _make_factory([run])
    await claim_and_publish(factory, mock_sqs)

    mock_sqs.send_message_batch.assert_called_once()
    entries = mock_sqs.send_message_batch.call_args[0][0]
    assert len(entries) == 1
    body = json.loads(entries[0]["MessageBody"])
    assert body["run_id"] == run.run_id
    assert body["job_id"] == run.job_id


@pytest.mark.asyncio
async def test_claim_and_publish_delay_seconds_past_run_is_zero(mock_sqs):
    """A run scheduled in the past gets DelaySeconds=0 (not negative)."""
    factory, _ = _make_factory([_make_run(1, 1, delta_seconds=-300)])
    await claim_and_publish(factory, mock_sqs)
    entries = mock_sqs.send_message_batch.call_args[0][0]
    assert entries[0]["DelaySeconds"] == 0


@pytest.mark.asyncio
async def test_claim_and_publish_delay_seconds_future_run(mock_sqs):
    """A run scheduled 2 min out gets DelaySeconds ≈ 120, within lookahead ceiling."""
    factory, _ = _make_factory([_make_run(1, 1, delta_seconds=120)])
    await claim_and_publish(factory, mock_sqs)
    entries = mock_sqs.send_message_batch.call_args[0][0]
    assert 100 <= entries[0]["DelaySeconds"] <= int(LOOKAHEAD.total_seconds())


@pytest.mark.asyncio
async def test_claim_and_publish_empty_returns_zero(mock_sqs):
    """When no runs are due, returns 0 and never calls SQS."""
    factory, _ = _make_factory([])
    count = await claim_and_publish(factory, mock_sqs)
    assert count == 0
    mock_sqs.send_message_batch.assert_not_called()


@pytest.mark.asyncio
async def test_claim_and_publish_sqs_failure_does_not_raise():
    """SQS failure after commit must not propagate — rows stay QUEUED for recovery."""
    factory, _ = _make_factory([_make_run(1, 1)])
    bad_sqs = MagicMock()
    bad_sqs.send_message_batch.side_effect = Exception("SQS unavailable")

    count = await claim_and_publish(factory, bad_sqs)
    assert count == 1  # claimed before the SQS error
