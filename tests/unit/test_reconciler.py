"""Unit tests for app/workers/reconciler.py — uses async mocks, no DB or SQS required."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.workers import reconciler as reconciler_module
from app.workers.reconciler import run_reconciler


def _async_cm(yield_value=None) -> MagicMock:
    """Create a MagicMock that acts as an async context manager yielding yield_value."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=yield_value)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _empty_result_factory() -> MagicMock:
    """A session_factory whose queries always return zero rows (both sweeps no-op)."""
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []

    session = MagicMock()
    session.execute = AsyncMock(return_value=mock_result)
    session.begin = MagicMock(return_value=_async_cm(None))

    return MagicMock(return_value=_async_cm(session))


@pytest.mark.asyncio
async def test_run_reconciler_uses_settings_interval_when_not_specified(monkeypatch):
    """No explicit ``interval`` → the loop sleeps for settings.reconciler_interval_seconds."""
    monkeypatch.setattr(reconciler_module.settings, "reconciler_interval_seconds", 42.0)

    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        raise asyncio.CancelledError

    factory = _empty_result_factory()
    sqs = MagicMock()

    with patch.object(reconciler_module.asyncio, "sleep", fake_sleep):
        with pytest.raises(asyncio.CancelledError):
            await run_reconciler(factory, sqs)

    assert sleep_calls == [42.0]


@pytest.mark.asyncio
async def test_run_reconciler_explicit_interval_overrides_settings(monkeypatch):
    """An explicit ``interval`` argument still wins over settings."""
    monkeypatch.setattr(reconciler_module.settings, "reconciler_interval_seconds", 42.0)

    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        raise asyncio.CancelledError

    factory = _empty_result_factory()
    sqs = MagicMock()

    with patch.object(reconciler_module.asyncio, "sleep", fake_sleep):
        with pytest.raises(asyncio.CancelledError):
            await run_reconciler(factory, sqs, interval=5.0)

    assert sleep_calls == [5.0]
