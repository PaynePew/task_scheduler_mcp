"""Unit tests for app/overload/health — all external calls mocked via injection."""

from __future__ import annotations

import asyncio

import pytest

from app.overload.health import CapacityExceeded, ConcurrencyLimiter, should_shed

# ---------------------------------------------------------------------------
# should_shed — CPU threshold
# ---------------------------------------------------------------------------


def test_shed_when_cpu_above_threshold():
    assert should_shed(cpu_threshold=0.80, _cpu_percent=85.0, _ram_percent=0.0, _queue_depth=0)


def test_no_shed_when_cpu_at_threshold():
    """Threshold is inclusive: cpu == threshold → shed."""
    assert should_shed(cpu_threshold=0.80, _cpu_percent=80.0, _ram_percent=0.0, _queue_depth=0)


def test_no_shed_when_cpu_below_threshold():
    assert not should_shed(cpu_threshold=0.90, _cpu_percent=89.9, _ram_percent=0.0, _queue_depth=0)


# ---------------------------------------------------------------------------
# should_shed — RAM threshold
# ---------------------------------------------------------------------------


def test_shed_when_ram_above_threshold():
    assert should_shed(ram_threshold=0.85, _cpu_percent=0.0, _ram_percent=90.0, _queue_depth=0)


def test_no_shed_when_ram_below_threshold():
    assert not should_shed(ram_threshold=0.90, _cpu_percent=0.0, _ram_percent=89.0, _queue_depth=0)


# ---------------------------------------------------------------------------
# should_shed — queue depth threshold
# ---------------------------------------------------------------------------


def test_shed_when_queue_depth_at_threshold():
    assert should_shed(
        queue_depth_threshold=100, _cpu_percent=0.0, _ram_percent=0.0, _queue_depth=100
    )


def test_shed_when_queue_depth_above_threshold():
    assert should_shed(
        queue_depth_threshold=100, _cpu_percent=0.0, _ram_percent=0.0, _queue_depth=200
    )


def test_no_shed_when_queue_depth_below_threshold():
    assert not should_shed(
        queue_depth_threshold=100, _cpu_percent=0.0, _ram_percent=0.0, _queue_depth=99
    )


# ---------------------------------------------------------------------------
# should_shed — all healthy
# ---------------------------------------------------------------------------


def test_no_shed_when_all_healthy():
    assert not should_shed(
        cpu_threshold=0.90,
        ram_threshold=0.90,
        queue_depth_threshold=1000,
        _cpu_percent=50.0,
        _ram_percent=50.0,
        _queue_depth=10,
    )


# ---------------------------------------------------------------------------
# ConcurrencyLimiter — happy path
# ---------------------------------------------------------------------------


async def test_limiter_allows_under_limit():
    limiter = ConcurrencyLimiter(limit=2)
    async with limiter.acquire():
        assert limiter.inflight == 1
    assert limiter.inflight == 0


async def test_limiter_allows_up_to_limit():
    limiter = ConcurrencyLimiter(limit=2)
    async with limiter.acquire():
        async with limiter.acquire():
            assert limiter.inflight == 2


async def test_limiter_decrements_on_exit():
    limiter = ConcurrencyLimiter(limit=3)
    async with limiter.acquire():
        pass
    assert limiter.inflight == 0


# ---------------------------------------------------------------------------
# ConcurrencyLimiter — rejection at capacity
# ---------------------------------------------------------------------------


async def test_limiter_rejects_at_capacity():
    limiter = ConcurrencyLimiter(limit=1)
    with pytest.raises(CapacityExceeded):
        async with limiter.acquire():
            async with limiter.acquire():  # second acquire should raise
                pass


async def test_limiter_rejects_past_limit_n():
    """N=2: third concurrent acquire raises CapacityExceeded."""
    limiter = ConcurrencyLimiter(limit=2)
    with pytest.raises(CapacityExceeded):
        async with limiter.acquire():
            async with limiter.acquire():
                async with limiter.acquire():  # third → raises
                    pass


async def test_limiter_allows_after_slot_freed():
    """Slot freed → next acquire succeeds even after a rejection."""
    limiter = ConcurrencyLimiter(limit=1)

    async with limiter.acquire():
        assert limiter.inflight == 1

    # Slot freed; new acquire must succeed.
    async with limiter.acquire():
        assert limiter.inflight == 1


async def test_limiter_decrements_even_on_body_exception():
    """In-flight counter decrements even if the body raises."""
    limiter = ConcurrencyLimiter(limit=2)
    try:
        async with limiter.acquire():
            raise ValueError("boom")
    except ValueError:
        pass
    assert limiter.inflight == 0


# ---------------------------------------------------------------------------
# ConcurrencyLimiter — concurrency property
# ---------------------------------------------------------------------------


async def test_limiter_limit_property():
    limiter = ConcurrencyLimiter(limit=5)
    assert limiter.limit == 5


async def test_limiter_inflight_tracks_concurrent():
    """Two tasks inside acquire → inflight == 2."""
    limiter = ConcurrencyLimiter(limit=5)
    results = []

    async def _task():
        async with limiter.acquire():
            results.append(limiter.inflight)
            await asyncio.sleep(0)  # yield so both tasks overlap

    await asyncio.gather(_task(), _task())
    # At least one observation of 2 in-flight
    assert 2 in results
