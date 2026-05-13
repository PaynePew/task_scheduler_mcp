"""Watcher — claims due PENDING runs, publishes to SQS, marks QUEUED.

Implements the loop from ADR-007 §Decision:
  1. SELECT ... FOR UPDATE SKIP LOCKED
  2. UPDATE job_runs SET status='QUEUED'
  3. INSERT run_events (QUEUED) per row
  4. COMMIT
  5. SQS send_message_batch AFTER commit (rows already QUEUED; SQS failure is safe)

Multiple Watcher processes are safe-by-construction: SKIP LOCKED ensures each
row is claimed by exactly one transaction.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import JobRun, RunEvent
from app.queue.sqs import SQSClient

logger = logging.getLogger(__name__)

LOOKAHEAD = timedelta(minutes=5)
DEFAULT_BATCH_SIZE = 10
DEFAULT_SLEEP_SECONDS = 5.0


async def claim_and_publish(
    session_factory: async_sessionmaker[AsyncSession],
    sqs: SQSClient,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> int:
    """One watcher tick: claim PENDING runs, write QUEUED transition, publish to SQS.

    Returns the number of runs claimed (0 if nothing was due).
    """
    now = datetime.now(tz=UTC)
    cutoff = now + LOOKAHEAD

    async with session_factory() as session:
        async with session.begin():
            # 1. Claim: SELECT PENDING runs due within the lookahead window, locking them.
            stmt = (
                select(JobRun)
                .where(
                    JobRun.status == "PENDING",
                    JobRun.scheduled_at < cutoff,
                )
                .with_for_update(skip_locked=True)
                .limit(batch_size)
            )
            result = await session.execute(stmt)
            runs = result.scalars().all()

            if not runs:
                return 0

            run_ids = [r.run_id for r in runs]

            # 2. UPDATE to QUEUED.
            await session.execute(
                update(JobRun)
                .where(JobRun.run_id.in_(run_ids))
                .values(status="QUEUED", updated_at=now)
            )

            # 3. INSERT RunEvent(QUEUED) per run — outbox pattern.
            for run in runs:
                session.add(
                    RunEvent(
                        run_id=run.run_id,
                        job_id=run.job_id,
                        event_type="QUEUED",
                        status_from="PENDING",
                        status_to="QUEUED",
                    )
                )
        # Transaction committed above. expire_on_commit=False keeps run attrs live.

        # 4. Build SQS entries — DelaySeconds set to message becomes visible at scheduled_at.
        entries = []
        for run in runs:
            delay = max(0, int((run.scheduled_at - now).total_seconds()))
            entries.append(
                {
                    "Id": str(run.run_id),
                    "MessageBody": json.dumps({"run_id": run.run_id, "job_id": run.job_id}),
                    "DelaySeconds": delay,
                }
            )

        # 5. Publish AFTER commit. If this fails, rows stay QUEUED; the reconciler
        #    (app/workers/reconciler.py sweep B) re-enqueues them — see issue #30.
        try:
            sqs.send_message_batch(entries)
        except Exception:
            logger.exception(
                "SQS send_message_batch failed for run_ids=%s; rows remain QUEUED", run_ids
            )

    return len(runs)


async def run_watcher(
    session_factory: async_sessionmaker[AsyncSession],
    sqs: SQSClient,
    *,
    interval: float = DEFAULT_SLEEP_SECONDS,
) -> None:
    """Watcher loop: claim → publish → sleep. Runs until cancelled."""
    logger.info("watcher: starting (interval=%.1fs, lookahead=%s)", interval, LOOKAHEAD)
    while True:
        try:
            count = await claim_and_publish(session_factory, sqs)
            if count:
                logger.info("watcher: claimed %d run(s)", count)
        except Exception:
            logger.exception("watcher tick error")
        await asyncio.sleep(interval)
