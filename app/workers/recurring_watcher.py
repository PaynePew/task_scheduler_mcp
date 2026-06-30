"""Continuation consumer — the single owner-driven reactor to terminal events.

ADR-067 unifies what used to be two consumers (``RecurringJobWatcher`` +
``ChainWatcher``) into one. Per terminal ``RunEvent`` (processed in ``event_id``
order, under one ``processed_by`` cursor) it asks the ``RunMaterializer`` to
create, in the *same transaction*:

  (a) the recurring successor — if the event's job is a recurring root and not
      cancelled (``materialize_successor``); and
  (b) one trigger-driven downstream run per matching downstream job
      (``materialize_downstream``).

This is **continuation**: a downstream run is created when its upstream run
reaches a terminal status — never pre-armed as a ``WAITING`` run. ``trigger_on_status``
is a **create predicate** (create the downstream iff the upstream's terminal
status matches), not the old flip predicate. A predicate miss creates no run and
records a lightweight audit ``RunEvent``; a slow consumer (downstream already
executing) is skipped, not created-then-cancelled, with an audit ``RunEvent``.

Exactly-once is a data-layer guarantee (ADR-067 §4): a redelivered terminal event
that retries a creation hits a unique index and is a no-op. The ``processed_by``
cursor stays only as an efficiency layer (skip already-handled events).

The legacy ``ChainWatcher`` remains deployed but idle — no ``WAITING`` run is
produced any more for it to flip; its removal is the next slice.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Job, JobRun, RunEvent
from app.domain.run_materializer import (
    ConcurrencyError,
    materialize_downstream,
    materialize_successor,
)

logger = logging.getLogger(__name__)

PROCESSED_BY_KEY = "continuation"
TERMINAL_EVENTS = ("SUCCEEDED", "FAILED", "CANCELLED")
DEFAULT_BATCH_SIZE = 50
DEFAULT_SLEEP_SECONDS = 5.0


def _triggers_on(trigger_on_status: str | None, event_type: str) -> bool:
    """Create predicate: does the upstream terminal status satisfy the trigger?

    ``SUCCEEDED`` / ``FAILED`` match only that terminal status; ``ANY`` (default
    None falls back to ``SUCCEEDED``) matches any terminal status, including
    CANCELLED. Mirrors the ADR-020 trigger semantics, now applied at *create*
    time rather than flip time (ADR-067 §2).
    """
    effective = trigger_on_status or "SUCCEEDED"
    return effective in ("ANY", event_type)


async def _materialize_recurring_successor(
    session: AsyncSession,
    *,
    event: RunEvent,
    upstream_job: Job,
) -> None:
    """Create the recurring successor for a recurring root's terminal event.

    No-op for cancelled jobs. Slow consumer (an executing run already exists) and
    a redelivered duplicate (unique index) are both skipped — exactly-once.
    """
    if upstream_job.cancelled_at is not None:
        logger.info(
            "continuation: job_id=%s is cancelled, skipping successor (run_id=%s event_type=%s)",
            upstream_job.job_id,
            event.run_id,
            event.event_type,
        )
        return

    prev_run = (
        await session.execute(
            select(JobRun).where(
                JobRun.run_id == event.run_id,
                JobRun.job_id == upstream_job.job_id,
            )
        )
    ).scalar_one()

    try:
        async with session.begin_nested():
            await materialize_successor(
                session,
                upstream_job,
                prev_run=prev_run,
                occurred_at=event.occurred_at,
            )
    except ConcurrencyError:
        # An executing successor already exists for this tick — skipping spawn.
        logger.info(
            "continuation: skipping spawn for job_id=%s"
            " (executing run already exists; run_id=%s event_type=%s)",
            upstream_job.job_id,
            event.run_id,
            event.event_type,
        )
    except IntegrityError:
        # A redelivered terminal event re-created an already-materialised tick;
        # the (job_id, scheduled_at) unique index made it a no-op — skipping spawn.
        logger.info(
            "continuation: skipping spawn for job_id=%s"
            " (duplicate successor for this tick; run_id=%s)",
            upstream_job.job_id,
            event.run_id,
        )


async def _materialize_downstreams(
    session: AsyncSession,
    *,
    event: RunEvent,
) -> None:
    """Create one trigger-driven downstream run per matching downstream job.

    For each active downstream (``trigger_on_job_id == event.job_id``,
    ``cancelled_at IS NULL``):
    - predicate miss → no run + ``CHAIN_SKIPPED_PREDICATE_MISS`` audit event;
    - slow consumer (downstream already executing) → skip + ``CHAIN_SKIPPED_SLOW_CONSUMER``;
    - redelivered duplicate (unique index) → no-op.
    """
    downstream_jobs = (
        (
            await session.execute(
                select(Job).where(
                    Job.trigger_on_job_id == event.job_id,
                    Job.cancelled_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )

    for downstream in downstream_jobs:
        if not _triggers_on(downstream.trigger_on_status, event.event_type):
            session.add(
                RunEvent(
                    run_id=event.run_id,
                    job_id=event.job_id,
                    event_type="CHAIN_SKIPPED_PREDICATE_MISS",
                    event_data={
                        "downstream_job_id": downstream.job_id,
                        "trigger_on_status": downstream.trigger_on_status,
                        "upstream_status": event.event_type,
                    },
                )
            )
            logger.info(
                "continuation: predicate miss — downstream job_id=%s trigger_on_status=%s"
                " does not match upstream %s (run_id=%s); no run created",
                downstream.job_id,
                downstream.trigger_on_status,
                event.event_type,
                event.run_id,
            )
            continue

        try:
            async with session.begin_nested():
                await materialize_downstream(
                    session,
                    downstream_job=downstream,
                    upstream_run_id=event.run_id,
                    occurred_at=event.occurred_at,
                )
        except ConcurrencyError:
            session.add(
                RunEvent(
                    run_id=event.run_id,
                    job_id=event.job_id,
                    event_type="CHAIN_SKIPPED_SLOW_CONSUMER",
                    event_data={
                        "downstream_job_id": downstream.job_id,
                        "upstream_status": event.event_type,
                    },
                )
            )
            logger.info(
                "continuation: slow-consumer drop — downstream job_id=%s already has an"
                " executing run; no run created for upstream run_id=%s",
                downstream.job_id,
                event.run_id,
            )
        except IntegrityError:
            logger.info(
                "continuation: skipping downstream for job_id=%s"
                " (duplicate for upstream run_id=%s; already created)",
                downstream.job_id,
                event.run_id,
            )


async def poll_once(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> int:
    """One consumer tick: process unprocessed terminal events in event_id order.

    For each event (causal order, ADR-067 §4): materialise the recurring successor
    (if any) and every matching downstream, then stamp the ``processed_by`` cursor
    so the event is never reprocessed. All work per event is atomic (within the
    enclosing ``session.begin()``).

    Returns the number of events processed (0 if nothing to do).
    """
    async with session_factory() as session:
        async with session.begin():
            events = (
                (
                    await session.execute(
                        select(RunEvent)
                        .where(
                            RunEvent.event_type.in_(TERMINAL_EVENTS),
                            # type: ignore: SQLAlchemy's JSONB.has_key is dynamically attached.
                            ~RunEvent.processed_by.has_key(PROCESSED_BY_KEY),  # type: ignore[attr-defined]
                        )
                        .order_by(RunEvent.event_id.asc())
                        .limit(batch_size)
                    )
                )
                .scalars()
                .all()
            )

            if not events:
                return 0

            now_iso = datetime.now(tz=UTC).isoformat()
            for event in events:
                upstream_job = (
                    await session.execute(select(Job).where(Job.job_id == event.job_id))
                ).scalar_one_or_none()

                # (a) Recurring successor — only for recurring roots.
                if upstream_job is not None and upstream_job.cron_expr is not None:
                    await _materialize_recurring_successor(
                        session, event=event, upstream_job=upstream_job
                    )

                # (b) Trigger-driven downstreams — for any upstream job type.
                await _materialize_downstreams(session, event=event)

                # Stamp the cursor so this event is never reprocessed on restart.
                new_pb = dict(event.processed_by)
                new_pb[PROCESSED_BY_KEY] = now_iso
                await session.execute(
                    update(RunEvent)
                    .where(RunEvent.event_id == event.event_id)
                    .values(processed_by=new_pb)
                )

    return len(events)


async def run_recurring_watcher(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    interval: float = DEFAULT_SLEEP_SECONDS,
) -> None:
    """Consumer loop: poll → materialise → sleep. Runs until cancelled."""
    logger.info("continuation consumer: starting (interval=%.1fs)", interval)
    while True:
        try:
            count = await poll_once(session_factory)
            if count:
                logger.info("continuation consumer: processed %d event(s)", count)
        except Exception:
            logger.exception("continuation consumer tick error")
        await asyncio.sleep(interval)
