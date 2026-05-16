"""ChainWatcher — outbox consumer for chained jobs.

Scans ``run_events`` for terminal events (SUCCEEDED / FAILED / CANCELLED),
finds WAITING ``job_runs`` whose ``wait_for_run_id`` matches the event's
``run_id``, and flips them to PENDING or CANCELLED based on the chained
Job's ``trigger_on_status`` field.

Match logic (ADR-020):
  trigger_on_status == event_type   → PENDING   (literal match)
  trigger_on_status == "ANY"        → PENDING   (any terminal, including CANCELLED)
  otherwise                         → CANCELLED (mismatch)
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Job, JobRun, RunEvent

logger = logging.getLogger(__name__)

PROCESSED_BY_KEY = "chain_watcher"
TERMINAL_EVENTS = ("SUCCEEDED", "FAILED", "CANCELLED")
DEFAULT_BATCH_SIZE = 50
DEFAULT_SLEEP_SECONDS = 5.0


def _is_match(trigger_on_status: str | None, event_type: str) -> bool:
    """Return True if the event type satisfies the job's trigger condition."""
    effective = trigger_on_status or "SUCCEEDED"
    return effective in ("ANY", event_type)


async def _flip_waiting_run(
    session: AsyncSession,
    *,
    waiting_run: JobRun,
    trigger_job: Job,
    event_type: str,
    now: datetime,
) -> None:
    """Flip one WAITING run to PENDING or CANCELLED and emit a RunEvent."""
    match = _is_match(trigger_job.trigger_on_status, event_type)
    new_status = "PENDING" if match else "CANCELLED"
    event_name = "QUEUED_BY_CHAIN" if match else "CANCELLED_BY_CHAIN_MISS"

    await session.execute(
        update(JobRun)
        .where(
            JobRun.run_id == waiting_run.run_id,
            JobRun.time_bucket == waiting_run.time_bucket,
            JobRun.status == "WAITING",
        )
        .values(status=new_status, updated_at=now)
    )
    session.add(
        RunEvent(
            run_id=waiting_run.run_id,
            job_id=waiting_run.job_id,
            event_type=event_name,
            status_from="WAITING",
            status_to=new_status,
        )
    )
    logger.info(
        "chain_watcher: flipped run_id=%s WAITING→%s (trigger_on_status=%s, event=%s)",
        waiting_run.run_id,
        new_status,
        trigger_job.trigger_on_status,
        event_type,
    )


async def poll_once(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> int:
    """One watcher tick: find unprocessed terminal events and flip matching WAITING runs.

    Returns the number of events processed (0 if nothing to do).
    """
    async with session_factory() as session:
        async with session.begin():
            # Fetch terminal run_events not yet stamped with our cursor.
            stmt = (
                select(RunEvent)
                .where(
                    RunEvent.event_type.in_(TERMINAL_EVENTS),
                    # type: ignore: SQLAlchemy's JSONB.has_key is dynamically attached.
                    ~RunEvent.processed_by.has_key(PROCESSED_BY_KEY),  # type: ignore[attr-defined]
                )
                .limit(batch_size)
            )
            events = (await session.execute(stmt)).scalars().all()

            if not events:
                return 0

            now = datetime.now(tz=UTC)
            now_iso = now.isoformat()

            for event in events:
                # Find all WAITING runs blocked on this run_id.
                waiting_runs = (
                    (
                        await session.execute(
                            select(JobRun).where(
                                JobRun.wait_for_run_id == event.run_id,
                                JobRun.status == "WAITING",
                            )
                        )
                    )
                    .scalars()
                    .all()
                )

                for waiting_run in waiting_runs:
                    # Load the chained job to read trigger_on_status.
                    trigger_job = (
                        await session.execute(select(Job).where(Job.job_id == waiting_run.job_id))
                    ).scalar_one_or_none()

                    if trigger_job is None:
                        logger.warning(
                            "chain_watcher: job_id=%s for waiting run_id=%s not found, skipping",
                            waiting_run.job_id,
                            waiting_run.run_id,
                        )
                        continue

                    await _flip_waiting_run(
                        session,
                        waiting_run=waiting_run,
                        trigger_job=trigger_job,
                        event_type=event.event_type,
                        now=now,
                    )

                # Stamp the cursor — atomic with the flips above (same transaction).
                new_pb = dict(event.processed_by)
                new_pb[PROCESSED_BY_KEY] = now_iso
                await session.execute(
                    update(RunEvent)
                    .where(RunEvent.event_id == event.event_id)
                    .values(processed_by=new_pb)
                )

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
