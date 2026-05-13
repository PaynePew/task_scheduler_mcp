"""RecurringJobWatcher — outbox consumer for recurring jobs.

Scans ``run_events`` for terminal events (SUCCEEDED / FAILED / CANCELLED) of
jobs whose ``cron_expr IS NOT NULL``, stamps the ``processed_by`` JSONB cursor
so the event is never reprocessed, and *logs* that it would spawn the next run.

Full cron expansion is deferred to W2:
    # TODO(W2): cron expansion — parse cron_expr, compute next fire time, insert JobRun
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Job, RunEvent

logger = logging.getLogger(__name__)

PROCESSED_BY_KEY = "recurring_watcher"
TERMINAL_EVENTS = ("SUCCEEDED", "FAILED", "CANCELLED")
DEFAULT_BATCH_SIZE = 50
DEFAULT_SLEEP_SECONDS = 5.0


async def poll_once(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> int:
    """One watcher tick: find unprocessed terminal events for recurring jobs, stamp them.

    Returns the number of events processed (0 if nothing to do).
    """
    async with session_factory() as session:
        async with session.begin():
            # Fetch terminal run_events for jobs with cron_expr, not yet stamped.
            # The JSONB `has_key` operator maps to Postgres `?` (key-exists).
            stmt = (
                select(RunEvent, Job.cron_expr)
                .join(Job, Job.job_id == RunEvent.job_id)
                .where(
                    RunEvent.event_type.in_(TERMINAL_EVENTS),
                    # type: ignore: SQLAlchemy's JSONB.has_key is dynamically attached.
                    ~RunEvent.processed_by.has_key(PROCESSED_BY_KEY),  # type: ignore[attr-defined]
                    Job.cron_expr.isnot(None),
                )
                .limit(batch_size)
            )
            rows = (await session.execute(stmt)).all()

            if not rows:
                return 0

            now_iso = datetime.now(tz=UTC).isoformat()
            for event, cron_expr in rows:
                logger.info(
                    "recurring_watcher: would have spawned next run for job_id=%s"
                    " run_id=%s event_type=%s cron_expr=%r",
                    event.job_id,
                    event.run_id,
                    event.event_type,
                    cron_expr,
                )
                # Stamp the cursor so this event is never reprocessed on restart.
                new_pb = dict(event.processed_by)
                new_pb[PROCESSED_BY_KEY] = now_iso
                await session.execute(
                    update(RunEvent)
                    .where(RunEvent.event_id == event.event_id)
                    .values(processed_by=new_pb)
                )
                # TODO(W2): cron expansion — parse cron_expr, compute next fire time, insert JobRun

    return len(rows)


async def run_recurring_watcher(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    interval: float = DEFAULT_SLEEP_SECONDS,
) -> None:
    """Watcher loop: poll → stamp → sleep. Runs until cancelled."""
    logger.info("recurring_watcher: starting (interval=%.1fs)", interval)
    while True:
        try:
            count = await poll_once(session_factory)
            if count:
                logger.info("recurring_watcher: processed %d event(s)", count)
        except Exception:
            logger.exception("recurring_watcher tick error")
        await asyncio.sleep(interval)
