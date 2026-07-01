"""Reconciler — closes the DB/queue gap for orphaned rows.

Two sweeps per tick (both use FOR UPDATE SKIP LOCKED per ADR-007 pattern):

  Sweep A — DLQ-orphaned RETRYING (issue #30 case A):
    Row left at RETRYING after MaxReceiveCount redeliveries exhaust the main
    queue and SQS routes the message to task-dlq.  Nobody updates the DB.
    Fix: flip RETRYING → FAILED + RunEvent(FAILED, reason='dlq_reconcile').

  Sweep B — QUEUED-stuck (ADR-007 deferred concern, now un-deferred):
    Watcher wrote QUEUED + committed, but sqs.send_message_batch raised.
    Fix: re-issue send_message_batch; on success, advance updated_at +
         RunEvent(REENQUEUED) so the row isn't re-swept next tick.
    Guard: only rows with scheduled_at < now are considered "stuck" — a row
    the Watcher legitimately queued ahead of time (within its 5-minute
    lookahead, with an SQS DelaySeconds) sits QUEUED with a stale updated_at
    on purpose. Without this guard Sweep B re-sends it with DelaySeconds=0,
    running it minutes early (issue #269).

Multiple reconciler processes are safe by the same SKIP LOCKED guarantee used
by the watcher (ADR-007).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config.settings import settings
from app.db.models import JobRun, RunEvent
from app.queue.sqs import SQSClient

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 100
_ZERO_TICK_LOG_INTERVAL = 60.0  # rate-limit "0 rows" INFO logs to once/minute


async def sweep_dlq_retrying(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    grace: timedelta,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> int:
    """Sweep A: flip RETRYING rows past the DLQ grace window to FAILED.

    Returns the number of rows reconciled.
    """
    cutoff = datetime.now(tz=UTC) - grace
    async with session_factory() as session:
        async with session.begin():
            result = await session.execute(
                select(JobRun)
                .where(JobRun.status == "RETRYING", JobRun.updated_at < cutoff)
                .with_for_update(skip_locked=True)
                .limit(batch_size)
            )
            runs = result.scalars().all()
            if not runs:
                return 0

            # Snapshot pre-UPDATE values: SQLAlchemy 2.x bulk update with the
            # default synchronize_session="auto" mutates loaded ORM instances
            # for columns in .values(...) — reading run.updated_at after the
            # UPDATE would return `now` instead of the actual stale timestamp.
            # (Project anti-pattern #1; see CODING_STANDARDS and Issue #10.)
            snapshots = [(r.run_id, r.job_id, r.updated_at) for r in runs]

            now = datetime.now(tz=UTC)
            run_ids = [run_id for run_id, _, _ in snapshots]
            await session.execute(
                update(JobRun)
                .where(JobRun.run_id.in_(run_ids))
                .values(
                    status="FAILED",
                    finish_at=now,
                    updated_at=now,
                    error_message="exceeded_max_receive (likely DLQ)",
                )
            )
            for run_id, job_id, last_updated_at in snapshots:
                logger.warning(
                    "reconciler sweep_a: RETRYING→FAILED run_id=%s job_id=%s last_updated_at=%s",
                    run_id,
                    job_id,
                    last_updated_at,
                )
                session.add(
                    RunEvent(
                        run_id=run_id,
                        job_id=job_id,
                        event_type="FAILED",
                        status_from="RETRYING",
                        status_to="FAILED",
                        event_data={
                            "reason": "dlq_reconcile",
                            "last_seen_at": last_updated_at.isoformat(),
                        },
                    )
                )
    return len(snapshots)


async def sweep_queued_stuck(
    session_factory: async_sessionmaker[AsyncSession],
    sqs: SQSClient,
    *,
    grace: timedelta,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> int:
    """Sweep B: re-enqueue QUEUED rows stuck past the grace window.

    Only rows that are genuinely past-due (``scheduled_at < now``) are
    eligible — a row the Watcher queued ahead of its `lookahead window` with
    an SQS ``DelaySeconds`` legitimately sits QUEUED with a stale
    ``updated_at`` until its scheduled time arrives (issue #269).

    Returns the number of rows successfully re-enqueued.
    """
    now = datetime.now(tz=UTC)
    cutoff = now - grace

    requeued = 0

    async with session_factory() as session:
        async with session.begin():
            result = await session.execute(
                select(JobRun)
                .where(
                    JobRun.status == "QUEUED",
                    JobRun.updated_at < cutoff,
                    JobRun.scheduled_at < now,
                )
                .with_for_update(skip_locked=True)
                .limit(batch_size)
            )
            runs = result.scalars().all()
            if not runs:
                return 0

            now = datetime.now(tz=UTC)
            for run in runs:
                try:
                    sqs.send_message_batch(
                        [
                            {
                                "Id": str(run.run_id),
                                "MessageBody": json.dumps(
                                    {"run_id": run.run_id, "job_id": run.job_id}
                                ),
                                "DelaySeconds": 0,
                            }
                        ]
                    )
                except Exception:
                    logger.exception(
                        "reconciler sweep_b: SQS send failed run_id=%s job_id=%s;"
                        " leaving for next tick",
                        run.run_id,
                        run.job_id,
                    )
                    continue

                logger.warning(
                    "reconciler sweep_b: QUEUED re-enqueued run_id=%s job_id=%s last_updated_at=%s",
                    run.run_id,
                    run.job_id,
                    run.updated_at,
                )
                await session.execute(
                    update(JobRun).where(JobRun.run_id == run.run_id).values(updated_at=now)
                )
                session.add(
                    RunEvent(
                        run_id=run.run_id,
                        job_id=run.job_id,
                        event_type="REENQUEUED",
                        status_from="QUEUED",
                        status_to="QUEUED",
                        event_data={"reason": "watcher_send_failed"},
                    )
                )
                requeued += 1

    return requeued


async def reconcile_once(
    session_factory: async_sessionmaker[AsyncSession],
    sqs: SQSClient,
    *,
    dlq_grace: timedelta | None = None,
    queued_grace: timedelta | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> tuple[int, int]:
    """Run one reconciler tick: both sweeps.

    Returns (sweep_a_count, sweep_b_count).
    """
    if dlq_grace is None:
        dlq_grace = timedelta(seconds=settings.reconciler_dlq_grace_seconds)
    if queued_grace is None:
        queued_grace = timedelta(seconds=settings.reconciler_queued_grace_seconds)

    a = await sweep_dlq_retrying(session_factory, grace=dlq_grace, batch_size=batch_size)
    b = await sweep_queued_stuck(session_factory, sqs, grace=queued_grace, batch_size=batch_size)
    return a, b


async def run_reconciler(
    session_factory: async_sessionmaker[AsyncSession],
    sqs: SQSClient,
    *,
    interval: float | None = None,
) -> None:
    """Reconciler loop: sweep → sleep. Runs until cancelled.

    ``interval`` defaults to ``settings.reconciler_interval_seconds`` (ADR-010:
    settings is the single source of truth) rather than a hardcoded constant.
    """
    if interval is None:
        interval = settings.reconciler_interval_seconds
    logger.info(
        "reconciler: starting (interval=%.0fs, dlq_grace=%ds, queued_grace=%ds)",
        interval,
        settings.reconciler_dlq_grace_seconds,
        settings.reconciler_queued_grace_seconds,
    )
    _last_zero_log: float = 0.0
    while True:
        try:
            a, b = await reconcile_once(session_factory, sqs)
            total = a + b
            if total > 0:
                logger.info("reconciler: tick reconciled sweep_a=%d sweep_b=%d", a, b)
            else:
                now = time.monotonic()
                if now - _last_zero_log >= _ZERO_TICK_LOG_INTERVAL:
                    logger.info("reconciler: tick 0 rows reconciled")
                    _last_zero_log = now
        except Exception:
            logger.exception("reconciler tick error")
        await asyncio.sleep(interval)
