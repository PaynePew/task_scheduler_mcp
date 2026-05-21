"""RecurringJobWatcher — outbox consumer for recurring jobs.

Scans ``run_events`` for terminal events (SUCCEEDED / FAILED / CANCELLED) of
jobs whose ``cron_expr IS NOT NULL``, stamps the ``processed_by`` JSONB cursor
so the event is never reprocessed, and inserts the next PENDING ``JobRun``.

Forbid-concurrency is intrinsic: terminal events drive spawning, so there is
never more than one pending/running run for a recurring job at any time.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import exists, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config.cron import next_after
from app.db.models import Job, JobRun, RunEvent

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
                    tz = ZoneInfo(job.timezone or "UTC")
                    # Anchor next-tick computation to max(occurred_at, prev_run.scheduled_at + 1µs).
                    # The Watcher claims PENDING runs a few hundred ms before scheduled_at
                    # (lookahead window), so a fast action can finish BEFORE its own
                    # scheduled tick boundary. Using occurred_at alone in next_after()
                    # (which has inclusive semantics) then re-spawns the SAME tick.
                    # Clamping to prev_run.scheduled_at + 1µs guarantees we always
                    # advance to a strictly later cron occurrence while preserving
                    # the "skip missed ticks" behavior when occurred_at is well past
                    # the scheduled tick (delayed-execution case).
                    prev_run = (
                        await session.execute(
                            select(JobRun).where(
                                JobRun.run_id == event.run_id,
                                JobRun.job_id == job.job_id,
                            )
                        )
                    ).scalar_one()
                    anchor = max(
                        event.occurred_at,
                        prev_run.scheduled_at + timedelta(microseconds=1),
                    )
                    run_at = next_after(job.cron_expr, tz, anchor)

                    # Forbid-concurrency pre-check (ADR-016 addendum): skip spawn
                    # if a non-terminal run already exists for this job. Within the
                    # same poll_once batch a prior iteration's flush (below) makes
                    # its newly-inserted PENDING row visible to this check, so two
                    # events that both compute the same run_at collapse to one spawn.
                    already_live = (
                        await session.execute(
                            select(
                                exists().where(
                                    JobRun.job_id == job.job_id,
                                    JobRun.status.in_(NON_TERMINAL_STATUSES),
                                )
                            )
                        )
                    ).scalar()

                    if already_live:
                        logger.info(
                            "recurring_watcher: skipping spawn for job_id=%s"
                            " (non-terminal run already exists; run_id=%s event_type=%s)",
                            job.job_id,
                            event.run_id,
                            event.event_type,
                        )
                    else:
                        time_bucket = run_at.replace(minute=0, second=0, microsecond=0).isoformat()

                        new_run = JobRun(
                            time_bucket=time_bucket,
                            job_id=job.job_id,
                            user_id=job.user_id,
                            scheduled_at=run_at,
                            status="PENDING",
                        )
                        session.add(new_run)
                        await session.flush()

                        new_event = RunEvent(
                            run_id=new_run.run_id,
                            job_id=job.job_id,
                            event_type="CREATED",
                        )
                        session.add(new_event)

                        logger.info(
                            "recurring_watcher: spawned run_id=%s for job_id=%s"
                            " scheduled_at=%s (cron=%r tz=%s)",
                            new_run.run_id,
                            job.job_id,
                            run_at.isoformat(),
                            job.cron_expr,
                            job.timezone,
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
