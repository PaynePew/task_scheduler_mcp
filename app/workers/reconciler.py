"""Reconciler — closes the DB/queue gap for orphaned rows.

Three sweeps per tick (all use FOR UPDATE SKIP LOCKED per ADR-007 pattern):

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

  Sweep C — RUNNING orphan (issue #271 / PRD #266):
    A worker that hard-crashes AFTER claiming a run (status='RUNNING') but
    BEFORE writing terminal leaves the row stuck: the redelivered message
    cannot re-claim it (the claim rejects RUNNING) and gets deleted, so nothing
    recovers it. Under forbid-concurrency (has_executing_run) this permanently
    wedges the job's recurrence/chaining and leaks its active_total quota slot
    (Job.state never settles). Detection keys on the DB-side heartbeat lease
    (job_runs.heartbeat_at, issue #267): a live long-running worker re-bumps it
    every ~30s, so a lease older than ``running_grace`` means a dead worker.
    Recovery is keyed on the action's idempotency posture (issue #268):
      - non-idempotent → FAILED + RunEvent(FAILED, reason='running_orphan') +
        settle (frees quota) + an operator-visible alert;
      - idempotent → reset to a claimable status (QUEUED) and re-enqueue (the
        reset must land before the re-sent message is visible, else it hits
        RUNNING again and re-orphans).

Multiple reconciler processes are safe by the same SKIP LOCKED guarantee used
by the watcher (ADR-007).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.actions.base import ActionHandler
from app.actions.registry import ACTION_REGISTRY
from app.config.settings import settings
from app.db.models import Job, JobRun, RunEvent
from app.domain.jobs import settle_job
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


async def sweep_running_orphan(
    session_factory: async_sessionmaker[AsyncSession],
    sqs: SQSClient,
    *,
    grace: timedelta,
    batch_size: int = DEFAULT_BATCH_SIZE,
    registry: dict[str, ActionHandler] | None = None,
) -> int:
    """Sweep C: recover RUNNING rows whose heartbeat lease is stale (dead worker).

    A row is an orphan when ``status='RUNNING'`` and its ``heartbeat_at`` lease is
    older than ``grace`` (or NULL — a RUNNING row with no lease cannot be
    attributed to a live worker). ``grace`` must exceed a live worker's worst-case
    lease staleness (``reconciler_running_grace_seconds``), so a slow-but-alive
    long-running action is never swept.

    Recovery is keyed on the action's declared idempotency posture (issue #268):
      - non-idempotent → FAILED + RunEvent(FAILED, reason='running_orphan') +
        ``settle_job`` (settles a one-shot/immediate and frees its quota; a no-op
        for recurring/chained, whose terminal event drives continuation) + an
        operator-visible ``logger.error`` alert.
      - idempotent → reset to a claimable status (QUEUED) and re-enqueue. The
        reset is committed *before* the message is sent (below), because a
        redelivered message that hits a still-RUNNING row would be deleted as a
        duplicate and re-orphan the run (PRD #266).

    Returns the number of rows recovered (failed + reset).
    """
    if registry is None:
        registry = ACTION_REGISTRY
    cutoff = datetime.now(tz=UTC) - grace
    to_reenqueue: list[tuple[int, int]] = []
    recovered = 0

    async with session_factory() as session:
        async with session.begin():
            result = await session.execute(
                select(JobRun)
                .where(
                    JobRun.status == "RUNNING",
                    or_(JobRun.heartbeat_at < cutoff, JobRun.heartbeat_at.is_(None)),
                )
                .with_for_update(skip_locked=True)
                .limit(batch_size)
            )
            runs = result.scalars().all()
            if not runs:
                return 0

            # Snapshot pre-UPDATE values before any bulk UPDATE mutates the loaded
            # ORM instances (synchronize_session="auto"; project anti-pattern #1).
            snapshots = [(r.run_id, r.job_id, r.heartbeat_at) for r in runs]

            # Resolve each job's action once so we can look up its idempotency
            # posture; the recovery policy is data-driven off that flag, never a
            # hardcoded action-name check (ADR-013 amendment / issue #268).
            job_ids = {job_id for _, job_id, _ in snapshots}
            action_rows = await session.execute(
                select(Job.job_id, Job.action).where(Job.job_id.in_(job_ids))
            )
            job_actions = dict(action_rows.all())

            now = datetime.now(tz=UTC)
            for run_id, job_id, last_hb in snapshots:
                action = job_actions.get(job_id)
                handler = registry.get(action) if action is not None else None
                # Fail-safe: an unknown/missing action is treated as non-idempotent
                # (never blind-retry something we can't prove is safe to replay).
                idempotent = bool(handler.idempotent) if handler is not None else False
                last_hb_iso = last_hb.isoformat() if last_hb is not None else None
                event_data = {"reason": "running_orphan", "last_heartbeat_at": last_hb_iso}

                if idempotent:
                    # Reset to a claimable status; the actual re-enqueue happens
                    # post-commit (below) so the row is claimable before the
                    # message exists.
                    await session.execute(
                        update(JobRun)
                        .where(JobRun.run_id == run_id)
                        .values(status="QUEUED", updated_at=now)
                    )
                    session.add(
                        RunEvent(
                            run_id=run_id,
                            job_id=job_id,
                            event_type="REENQUEUED",
                            status_from="RUNNING",
                            status_to="QUEUED",
                            event_data=event_data,
                        )
                    )
                    to_reenqueue.append((run_id, job_id))
                    logger.warning(
                        "reconciler sweep_c: RUNNING orphan reset→QUEUED (idempotent) "
                        "run_id=%s job_id=%s action=%s last_heartbeat_at=%s",
                        run_id,
                        job_id,
                        action,
                        last_hb_iso,
                    )
                else:
                    await session.execute(
                        update(JobRun)
                        .where(JobRun.run_id == run_id)
                        .values(
                            status="FAILED",
                            finish_at=now,
                            updated_at=now,
                            error_message=(
                                "running_orphan: worker died mid-run (non-idempotent, not retried)"
                            ),
                        )
                    )
                    session.add(
                        RunEvent(
                            run_id=run_id,
                            job_id=job_id,
                            event_type="FAILED",
                            status_from="RUNNING",
                            status_to="FAILED",
                            event_data=event_data,
                        )
                    )
                    # Settle a one-shot/immediate in the same transaction as the
                    # terminal write (ADR-068) so its active_total quota slot frees;
                    # a no-op for recurring/chained, whose terminal RunEvent(FAILED)
                    # the continuation consumer picks up to un-wedge the job.
                    await settle_job(session, job_id=job_id)
                    logger.error(
                        "reconciler sweep_c ALERT: RUNNING orphan → FAILED (non-idempotent) "
                        "run_id=%s job_id=%s action=%s last_heartbeat_at=%s — worker died "
                        "mid-run; not retried (effect may or may not have happened)",
                        run_id,
                        job_id,
                        action,
                        last_hb_iso,
                    )
                recovered += 1

    # Transaction committed: reset rows are now claimable in the DB. Enqueue only
    # after commit so the message becomes visible only once the row can be claimed
    # — otherwise a fast worker could claim-fail on a still-RUNNING row and delete
    # the message, re-orphaning the run. A send failure here is safe: the row sits
    # QUEUED and Sweep B re-enqueues it on a later tick.
    for run_id, job_id in to_reenqueue:
        try:
            sqs.send_message_batch(
                [
                    {
                        "Id": str(run_id),
                        "MessageBody": json.dumps({"run_id": run_id, "job_id": job_id}),
                        "DelaySeconds": 0,
                    }
                ]
            )
        except Exception:
            logger.exception(
                "reconciler sweep_c: SQS re-enqueue failed run_id=%s job_id=%s;"
                " row is QUEUED and Sweep B will re-enqueue on a later tick",
                run_id,
                job_id,
            )

    return recovered


async def reconcile_once(
    session_factory: async_sessionmaker[AsyncSession],
    sqs: SQSClient,
    *,
    dlq_grace: timedelta | None = None,
    queued_grace: timedelta | None = None,
    running_grace: timedelta | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> tuple[int, int, int]:
    """Run one reconciler tick: all three sweeps.

    Returns (sweep_a_count, sweep_b_count, sweep_c_count).
    """
    if dlq_grace is None:
        dlq_grace = timedelta(seconds=settings.reconciler_dlq_grace_seconds)
    if queued_grace is None:
        queued_grace = timedelta(seconds=settings.reconciler_queued_grace_seconds)
    if running_grace is None:
        running_grace = timedelta(seconds=settings.reconciler_running_grace_seconds)

    a = await sweep_dlq_retrying(session_factory, grace=dlq_grace, batch_size=batch_size)
    b = await sweep_queued_stuck(session_factory, sqs, grace=queued_grace, batch_size=batch_size)
    c = await sweep_running_orphan(session_factory, sqs, grace=running_grace, batch_size=batch_size)
    return a, b, c


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
        "reconciler: starting (interval=%.0fs, dlq_grace=%ds, queued_grace=%ds, running_grace=%ds)",
        interval,
        settings.reconciler_dlq_grace_seconds,
        settings.reconciler_queued_grace_seconds,
        settings.reconciler_running_grace_seconds,
    )
    _last_zero_log: float = 0.0
    while True:
        try:
            a, b, c = await reconcile_once(session_factory, sqs)
            total = a + b + c
            if total > 0:
                logger.info("reconciler: tick reconciled sweep_a=%d sweep_b=%d sweep_c=%d", a, b, c)
            else:
                now = time.monotonic()
                if now - _last_zero_log >= _ZERO_TICK_LOG_INTERVAL:
                    logger.info("reconciler: tick 0 rows reconciled")
                    _last_zero_log = now
        except Exception:
            logger.exception("reconciler tick error")
        await asyncio.sleep(interval)
