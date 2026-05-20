"""Health-based load shedding and concurrency limiting (ADR-057).

Public surface:
  - should_shed()        — returns True when CPU / RAM / queue thresholds are breached.
  - get_queue_depth()    — cached SQS queue depth (30 s TTL).
  - ConcurrencyLimiter   — asyncio-safe in-flight request cap.
  - CapacityExceeded     — raised by ConcurrencyLimiter when the limit is reached.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator

import psutil

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Queue depth — cached at module level to avoid per-request SQS calls
# ---------------------------------------------------------------------------

_QUEUE_DEPTH_TTL: float = 30.0  # seconds
_queue_depth_cache: dict[str, tuple[int, float]] = {}  # url → (depth, monotonic_ts)


def get_queue_depth(queue_url: str) -> int:
    """Return the approximate SQS queue depth, cached for _QUEUE_DEPTH_TTL seconds.

    Uses SQSClient under the hood; returns 0 on any error so that a broken
    queue endpoint never causes spurious load shedding or false backpressure.
    """
    now = time.monotonic()
    if queue_url in _queue_depth_cache:
        depth, ts = _queue_depth_cache[queue_url]
        if now - ts < _QUEUE_DEPTH_TTL:
            return depth

    from app.queue.sqs import SQSClient

    depth = SQSClient(queue_url=queue_url).get_queue_depth()
    _queue_depth_cache[queue_url] = (depth, now)
    return depth


# ---------------------------------------------------------------------------
# Concurrency limiter
# ---------------------------------------------------------------------------


class CapacityExceeded(Exception):
    """Raised by ConcurrencyLimiter when the in-flight limit is reached."""


class ConcurrencyLimiter:
    """asyncio-safe in-flight request cap sized by OVERLOAD_CONCURRENCY_LIMIT.

    Uses an explicit counter + lock rather than asyncio.Semaphore so that
    we can reject immediately (non-blocking) instead of queuing waiters.
    """

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._inflight = 0
        self._lock = asyncio.Lock()

    @property
    def inflight(self) -> int:
        return self._inflight

    @property
    def limit(self) -> int:
        return self._limit

    @contextlib.asynccontextmanager
    async def acquire(self) -> AsyncIterator[None]:
        """Acquire one slot or raise CapacityExceeded immediately if at limit."""
        async with self._lock:
            if self._inflight >= self._limit:
                raise CapacityExceeded(
                    f"Concurrency limit {self._limit} reached ({self._inflight} requests in flight)"
                )
            self._inflight += 1
        try:
            yield
        finally:
            async with self._lock:
                self._inflight -= 1


# ---------------------------------------------------------------------------
# Load-shedding decision
# ---------------------------------------------------------------------------


def should_shed(
    *,
    cpu_threshold: float | None = None,
    ram_threshold: float | None = None,
    queue_depth_threshold: int | None = None,
    queue_url: str | None = None,
    _cpu_percent: float | None = None,
    _ram_percent: float | None = None,
    _queue_depth: int | None = None,
) -> bool:
    """Return True when any health threshold is breached.

    Thresholds default to the settings env vars; pass explicit values in tests.

    ``_cpu_percent``, ``_ram_percent``, and ``_queue_depth`` are injectable
    for unit-test determinism — production code leaves them None so live
    psutil / SQS measurements are used.

    CPU / RAM thresholds are fractions in [0, 1] (e.g. 0.90 = 90%).
    Queue depth threshold is an absolute message count.
    """
    from app.config.settings import settings

    cpu_limit = cpu_threshold if cpu_threshold is not None else settings.overload_cpu_threshold
    ram_limit = ram_threshold if ram_threshold is not None else settings.overload_ram_threshold
    depth_limit = (
        queue_depth_threshold
        if queue_depth_threshold is not None
        else settings.overload_queue_depth_threshold
    )
    q_url = queue_url or settings.queue_url

    # psutil returns percent in [0, 100]; threshold is [0, 1] fraction.
    cpu = _cpu_percent if _cpu_percent is not None else psutil.cpu_percent(interval=None)
    if cpu >= cpu_limit * 100:
        logger.warning("load shedding: cpu=%.1f%% >= threshold=%.0f%%", cpu, cpu_limit * 100)
        return True

    ram = _ram_percent if _ram_percent is not None else psutil.virtual_memory().percent
    if ram >= ram_limit * 100:
        logger.warning("load shedding: ram=%.1f%% >= threshold=%.0f%%", ram, ram_limit * 100)
        return True

    depth = _queue_depth if _queue_depth is not None else get_queue_depth(q_url)
    if depth >= depth_limit:
        logger.warning("load shedding: queue_depth=%d >= threshold=%d", depth, depth_limit)
        return True

    return False
