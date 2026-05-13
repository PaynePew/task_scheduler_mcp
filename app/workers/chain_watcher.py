"""ChainWatcher — outbox consumer for chained jobs.

Scans ``run_events`` for terminal events (SUCCEEDED / FAILED / CANCELLED) of
jobs that are referenced as ``trigger_on_job_id`` by another job, stamps the
``processed_by`` JSONB cursor, and *logs* that it would flip matching WAITING
runs.

Full WAITING → PENDING/CANCELLED flip is deferred to W2:
    # TODO(W2): WAITING → PENDING/CANCELLED flip — query job_runs WHERE
    #           wait_for_run_id = :run_id AND status = 'WAITING' and flip based
    #           on trigger_on_status vs actual terminal event_type.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy import exists, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Job, RunEvent

logger = logging.getLogger(__name__)

PROCESSED_BY_KEY = "chain_watcher"
TERMINAL_EVENTS = ("SUCCEEDED", "FAILED", "CANCELLED")
DEFAULT_BATCH_SIZE = 50
DEFAULT_SLEEP_SECONDS = 5.0


async def poll_once(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> int:
    """One watcher tick: find unprocessed terminal events that unblock chained jobs.

    Returns the number of events processed (0 if nothing to do).
    """
    async with session_factory() as session:
        async with session.begin():
            # Fetch terminal run_events whose job_id is a trigger for at least
            # one other job (i.e. there is a Job with trigger_on_job_id = event.job_id).
            triggered_alias = Job.__table__.alias("triggered")
            has_downstream = exists().where(triggered_alias.c.trigger_on_job_id == RunEvent.job_id)
            stmt = (
                select(RunEvent)
                .where(
                    RunEvent.event_type.in_(TERMINAL_EVENTS),
                    # type: ignore: SQLAlchemy's JSONB.has_key is dynamically attached.
                    ~RunEvent.processed_by.has_key(PROCESSED_BY_KEY),  # type: ignore[attr-defined]
                    has_downstream,
                )
                .limit(batch_size)
            )
            events = (await session.execute(stmt)).scalars().all()

            if not events:
                return 0

            now_iso = datetime.now(tz=UTC).isoformat()
            for event in events:
                logger.info(
                    "chain_watcher: would have flipped WAITING runs triggered by"
                    " job_id=%s run_id=%s event_type=%s",
                    event.job_id,
                    event.run_id,
                    event.event_type,
                )
                # Stamp the cursor so this event is never reprocessed on restart.
                new_pb = dict(event.processed_by)
                new_pb[PROCESSED_BY_KEY] = now_iso
                await session.execute(
                    update(RunEvent)
                    .where(RunEvent.event_id == event.event_id)
                    .values(processed_by=new_pb)
                )
                # TODO(W2): WAITING → PENDING/CANCELLED flip — query job_runs WHERE
                #           wait_for_run_id = :run_id AND status = 'WAITING' and flip
                #           based on trigger_on_status vs actual terminal event_type.

    return len(events)


async def run_chain_watcher(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    interval: float = DEFAULT_SLEEP_SECONDS,
) -> None:
    """Watcher loop: poll → stamp → sleep. Runs until cancelled."""
    logger.info("chain_watcher: starting (interval=%.1fs)", interval)
    while True:
        try:
            count = await poll_once(session_factory)
            if count:
                logger.info("chain_watcher: processed %d event(s)", count)
        except Exception:
            logger.exception("chain_watcher tick error")
        await asyncio.sleep(interval)
