"""Worker executor — happy path (claim, dispatch, terminal).

S07a scope (per issue #7):
- receive → claim → dispatch → SUCCEEDED (ok=True) → DeleteMessage
- receive → claim → dispatch → FAILED (ok=False, retryable=False) → DeleteMessage
- ok=False, retryable=True: log + leave message (S07b adds retry/heartbeat)
- exception / timeout: log + leave message (SQS visibility expiry handles redelivery)

The claim is atomic: UPDATE ... WHERE status IN ('PENDING','QUEUED') RETURNING ...
Only one worker wins on duplicate delivery; the other no-ops and DeleteMessages.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.actions.base import ActionHandler, ActionResult
from app.actions.registry import ACTION_REGISTRY
from app.db.models import Job, JobRun, RunEvent
from app.queue.sqs import SQSClient

logger = logging.getLogger(__name__)

POLL_WAIT_SECONDS = 20


async def _claim(
    session: AsyncSession,
    run_id: int,
    job_id: int,
) -> bool:
    """Atomic claim: UPDATE status→RUNNING + insert RunEvent(STARTED).

    Returns True if this worker won the claim, False if already claimed.
    """
    now = datetime.now(tz=UTC)
    result = await session.execute(
        update(JobRun)
        .where(
            JobRun.run_id == run_id,
            JobRun.status.in_(["PENDING", "QUEUED"]),
        )
        .values(status="RUNNING", start_at=now, updated_at=now)
        .returning(JobRun.run_id, JobRun.job_id)
    )
    if result.first() is None:
        return False

    session.add(
        RunEvent(
            run_id=run_id,
            job_id=job_id,
            event_type="STARTED",
            status_from="QUEUED",
            status_to="RUNNING",
        )
    )
    return True


async def _write_terminal(
    session: AsyncSession,
    run_id: int,
    job_id: int,
    terminal_status: str,
    action_result: ActionResult,
) -> None:
    """Write terminal status + RunEvent in one transaction (called inside session.begin())."""
    now = datetime.now(tz=UTC)
    await session.execute(
        update(JobRun)
        .where(JobRun.run_id == run_id)
        .values(
            status=terminal_status,
            finish_at=now,
            updated_at=now,
            result=json.dumps(action_result.result) if action_result.result is not None else None,
            error_message=action_result.error,
        )
    )
    session.add(
        RunEvent(
            run_id=run_id,
            job_id=job_id,
            event_type=terminal_status,
            status_from="RUNNING",
            status_to=terminal_status,
            event_data=action_result.result,
        )
    )


async def process_one(
    session_factory: async_sessionmaker[AsyncSession],
    sqs: SQSClient,
    message: dict,
    *,
    registry: dict[str, ActionHandler] | None = None,
) -> None:
    """Process a single SQS message: claim → dispatch → terminal.

    On success or permanent failure: DeleteMessage.
    On retryable failure or exception: leave message for SQS visibility expiry.
    """
    if registry is None:
        registry = ACTION_REGISTRY

    body = json.loads(message["Body"])
    run_id: int = body["run_id"]
    job_id: int = body["job_id"]
    receipt: str = message["ReceiptHandle"]

    # Phase 1: atomic claim + load job/run in one transaction
    claimed = False
    job: Job | None = None
    run: JobRun | None = None

    async with session_factory() as session:
        async with session.begin():
            claimed = await _claim(session, run_id, job_id)
            if not claimed:
                logger.info("run_id=%s already claimed by another worker; skipping", run_id)
            else:
                job = await session.get(Job, job_id)
                run_result = await session.execute(select(JobRun).where(JobRun.run_id == run_id))
                run = run_result.scalar_one_or_none()

    if not claimed:
        sqs.delete_message(receipt)
        return

    if job is None or run is None:
        logger.error("run_id=%s: job or run not found after claim; leaving message", run_id)
        return

    # Phase 2: resolve handler + parse params
    handler = registry.get(job.action)
    if handler is None:
        logger.error("run_id=%s: unknown action %r; leaving message", run_id, job.action)
        return

    try:
        params = handler.params_model.model_validate(job.action_params)
    except Exception:
        logger.exception("run_id=%s: params validation failed; leaving message", run_id)
        return

    # Phase 3: dispatch with per-action timeout (S07a: exception/timeout → leave message)
    try:
        action_result = await asyncio.wait_for(
            handler.execute(run, params),
            timeout=handler.timeout_seconds,
        )
    except Exception:
        logger.exception(
            "run_id=%s action=%r raised or timed out; leaving message for redelivery",
            run_id,
            job.action,
        )
        return

    # Phase 4: write terminal status in one transaction + DeleteMessage
    if action_result.ok:
        terminal_status = "SUCCEEDED"
    elif not action_result.retryable:
        terminal_status = "FAILED"
    else:
        # retryable=True — S07b handles retry/heartbeat; S07a leaves message
        logger.info(
            "run_id=%s action=%r returned retryable failure; leaving message (S07b)",
            run_id,
            job.action,
        )
        return

    async with session_factory() as session:
        async with session.begin():
            await _write_terminal(session, run_id, job_id, terminal_status, action_result)

    sqs.delete_message(receipt)
    logger.info("run_id=%s → %s", run_id, terminal_status)


async def run_worker(
    session_factory: async_sessionmaker[AsyncSession],
    sqs: SQSClient,
    *,
    registry: dict[str, ActionHandler] | None = None,
    poll_wait: int = POLL_WAIT_SECONDS,
) -> None:
    """Worker loop: poll → process → repeat. Runs until cancelled."""
    logger.info("worker: starting (poll_wait=%ds)", poll_wait)
    while True:
        try:
            messages = sqs.receive_messages(
                max_messages=1,
                wait_seconds=poll_wait,
                visibility_timeout=60,
            )
            for msg in messages:
                try:
                    await process_one(session_factory, sqs, msg, registry=registry)
                except Exception:
                    logger.exception("unhandled error for message %s", msg.get("MessageId"))
        except Exception:
            logger.exception("worker poll error")
