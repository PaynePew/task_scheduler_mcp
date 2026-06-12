"""RecurringJobWatcher — outbox consumer for recurring jobs.

Scans ``run_events`` for terminal events (SUCCEEDED / FAILED / CANCELLED) of
jobs whose ``cron_expr IS NOT NULL``, stamps the ``processed_by`` JSONB cursor
so the event is never reprocessed, and inserts the next PENDING ``JobRun`` via
``RunMaterializer.materialize_successor`` (ADR-065).

Forbid-concurrency is intrinsic: terminal events drive spawning, so there is
never more than one pending/running run for a recurring job at any time.
Materialization also arms downstream WAITING runs in the same transaction,
ensuring each tick of a recurring A produces a fresh WAITING run for chained B.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Job, JobRun, RunEvent
from app.domain.run_materializer import ConcurrencyError, materialize_successor

logger = logging.getLogger(__name__)

PROCESSED_BY_KEY = "recurring_watcher"
TERMINAL_EVENTS = ("SUCCEEDED", "FAILED", "CANCELLED")
NON_TERMINAL_STATUSES = ("PENDING", "QUEUED", "WAITING", "RUNNING", "RETRYING")
DEFAULT_BATCH_SIZE = 50
DEFAULT_SLEEP_SECONDS = 5.0


async def poll_once(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> int:
    """One watcher tick: process unprocessed terminal events for recurring jobs.

    For each event:
    - If the Job has been cancelled (cancelled_at IS NOT NULL), stamp processed
      but do NOT insert a next run.
    - Otherwise compute next_after(cron_expr, tz, event.occurred_at), insert
      one PENDING JobRun, then stamp the event processed.

    All work per event is atomic (within the enclosing session.begin()).

    Returns the number of events processed (0 if nothing to do).
    """
    async with session_factory() as session:
        async with session.begin():
            stmt = (
                select(RunEvent, Job)
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
            for event, job in rows:
                if job.cancelled_at is not None:
                    logger.info(
                        "recurring_watcher: job_id=%s is cancelled, skipping spawn"
                        " (run_id=%s event_type=%s)",
                        job.job_id,
                        event.run_id,
                        event.event_type,
                    )
                else:
                    # Load prev_run to anchor the next-tick calculation.
                    # Anchor: max(occurred_at, prev_run.scheduled_at + 1µs) — prevents
                    # re-spawning the same tick when the action finishes BEFORE its own
                    # scheduled_at (Watcher's lookahead window claims early).
                    prev_run = (
                        await session.execute(
                            select(JobRun).where(
                                JobRun.run_id == event.run_id,
                                JobRun.job_id == job.job_id,
                            )
                        )
                    ).scalar_one()

                    try:
                        # materialize_successor: next cron occurrence + arm downstream
                        # in the same transaction (ADR-065).
                        # ConcurrencyError = executing run already exists → same
                        # skip semantics as the old already_live pre-check.
                        await materialize_successor(
                            session,
                            job,
                            prev_run=prev_run,
                            occurred_at=event.occurred_at,
                        )
                    except ConcurrencyError:
                        logger.info(
                            "recurring_watcher: skipping spawn for job_id=%s"
                            " (executing run already exists; run_id=%s event_type=%s)",
                            job.job_id,
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

    return len(rows)


async def run_recurring_watcher(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    interval: float = DEFAULT_SLEEP_SECONDS,
) -> None:
    """Watcher loop: poll → spawn → sleep. Runs until cancelled."""
    logger.info("recurring_watcher: starting (interval=%.1fs)", interval)
    while True:
        try:
            count = await poll_once(session_factory)
            if count:
                logger.info("recurring_watcher: processed %d event(s)", count)
        except Exception:
            logger.exception("recurring_watcher tick error")
        await asyncio.sleep(interval)
