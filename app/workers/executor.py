"""Worker executor — claim, dispatch, terminal.

S07a scope (per issue #7):
- receive → claim → dispatch → SUCCEEDED (ok=True) → DeleteMessage
- receive → claim → dispatch → FAILED (ok=False, retryable=False) → DeleteMessage
- ok=False, retryable=True: log + leave message (S07b adds retry/heartbeat)
- exception / timeout: log + leave message (SQS visibility expiry handles redelivery)

Issue #26 — pre-dispatch permanent failures (write FAILED + DeleteMessage so
the message exits the queue instead of infinite-looping via redelivery):
- job/row missing after successful claim → DeleteMessage only (no row to update)
- unknown action → FAILED + DeleteMessage
- params validation raises → FAILED + DeleteMessage

The claim is atomic: UPDATE ... WHERE status IN ('PENDING','QUEUED') RETURNING ...
Only one worker wins on duplicate delivery; the other no-ops and DeleteMessages.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.actions.base import ActionHandler, ActionResult
from app.actions.registry import ACTION_REGISTRY
from app.db.models import Job, JobRun, RunEvent
from app.obs.logging import bind_job_id, bind_run_id, unbind_job_id, unbind_run_id
from app.queue.sqs import SQSClient

logger = logging.getLogger(__name__)

POLL_WAIT_SECONDS = 20
HEARTBEAT_INTERVAL_SECONDS = 30
HEARTBEAT_EXTENSION_SECONDS = 60


async def _claim(
    session: AsyncSession,
    run_id: int,
    job_id: int,
) -> bool:
    """Atomic claim: UPDATE status→RUNNING + insert RunEvent(STARTED).

    Accepts PENDING/QUEUED (first delivery) and RETRYING (redelivery after a
    retryable failure) — without RETRYING, the next redelivery would fail to
    claim and the message would be deleted as if already-processed.

    Returns True if this worker won the claim, False if already claimed.
    """
    now = datetime.now(tz=UTC)
    prev_status = (
        await session.execute(select(JobRun.status).where(JobRun.run_id == run_id))
    ).scalar_one_or_none()

    result = await session.execute(
        update(JobRun)
        .where(
            JobRun.run_id == run_id,
            JobRun.status.in_(["PENDING", "QUEUED", "RETRYING"]),
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
            status_from=prev_status or "QUEUED",
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
            error_code=action_result.error_code,
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


async def _write_permanent_failure(
    session: AsyncSession,
    run_id: int,
    job_id: int,
    error_message: str,
    event_data: dict | None = None,
) -> None:
    """Write FAILED terminal status + RunEvent for a permanently-invalid job.

    Called inside session.begin(). Does not DeleteMessage — caller is responsible.
    """
    now = datetime.now(tz=UTC)
    await session.execute(
        update(JobRun)
        .where(JobRun.run_id == run_id)
        .values(status="FAILED", finish_at=now, updated_at=now, error_message=error_message)
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


async def _write_retrying(
    session: AsyncSession,
    run_id: int,
    job_id: int,
    error_message: str,
) -> None:
    """Write RETRYING status + RunEvent(RETRY) in one transaction.
    Caller does NOT DeleteMessage - SQS visibility expiry handles redelivery,
    and after MaxReceiveCount = 3 the message routes to the DLQ.
    """
    now = datetime.now(tz=UTC)
    await session.execute(
        update(JobRun)
        .where(JobRun.run_id == run_id)
        .values(
            status="RETRYING",
            updated_at=now,
            error_message=error_message,
        )
    )
    session.add(
        RunEvent(
            run_id=run_id,
            job_id=job_id,
            event_type="RETRY",
            status_from="RUNNING",
            status_to="RETRYING",
        )
    )


async def _heartbeat_loop(
    sqs: SQSClient,
    receipt_handle: str,
    *,
    interval: float = HEARTBEAT_INTERVAL_SECONDS,
    extension: int = HEARTBEAT_EXTENSION_SECONDS,
) -> None:
    """Extend SQS visibility while a long action runs.

    Designed to be run as a background Task and cancelled by the caller
    when the action returns. Failures are logged but not raised - if SQS
    becomes unreachable the visibility expires naturally and the message
    redelivers, which is the correct fallback.
    """
    while True:
        await asyncio.sleep(interval)
        try:
            sqs.change_message_visibility(
                receipt_handle,
                visibility_timeout=extension,
            )
        except Exception:
            logger.exception(
                "heartbeat failed for receipt=%s; visibility may expire",
                receipt_handle,
            )


@asynccontextmanager
async def _heartbeat(
    sqs: SQSClient,
    receipt_handle: str,
    *,
    interval: float = HEARTBEAT_INTERVAL_SECONDS,
):
    """Run _heartbeat_loop in the background for the `async with` block,
    Cancels cleanly on exit (success, exception, or outer cancellation),
    so no asyncio task leaks.
    """
    task = asyncio.create_task(_heartbeat_loop(sqs, receipt_handle, interval=interval))
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def process_one(
    session_factory: async_sessionmaker[AsyncSession],
    sqs: SQSClient,
    message: dict,
    *,
    registry: dict[str, ActionHandler] | None = None,
    heartbeat_interval: float = HEARTBEAT_INTERVAL_SECONDS,
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

    tok_job = bind_job_id(str(job_id))
    tok_run = bind_run_id(str(run_id))
    try:
        await _dispatch(
            session_factory, sqs, run_id, job_id, receipt, message, registry, heartbeat_interval
        )
    finally:
        unbind_run_id(tok_run)
        unbind_job_id(tok_job)


async def _dispatch(
    session_factory: async_sessionmaker[AsyncSession],
    sqs: SQSClient,
    run_id: int,
    job_id: int,
    receipt: str,
    message: dict,
    registry: dict[str, ActionHandler] | None,
    heartbeat_interval: float,
) -> None:
    """Inner dispatch after correlation IDs are bound to the log context."""
    receive_count = message.get("Attributes", {}).get("ApproximateReceiveCount", "?")
    logger.info(
        "processing run_id=%s job_id=%s msg=%s receive_count=%s",
        run_id,
        job_id,
        message.get("MessageId"),
        receive_count,
    )

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
        # Row was deleted between claim and load — can't write terminal state.
        logger.error(
            "run_id=%s job_id=%s: job/run row missing after successful claim; deleting message",
            run_id,
            job_id,
        )
        sqs.delete_message(receipt)
        return

    # Phase 2: resolve handler + parse params
    handler = registry.get(job.action)
    if handler is None:
        logger.warning(
            "run_id=%s job_id=%s action=%r: unknown action; marking FAILED",
            run_id,
            job_id,
            job.action,
        )
        async with session_factory() as session:
            async with session.begin():
                await _write_permanent_failure(
                    session,
                    run_id,
                    job_id,
                    error_message=f"unknown action: {job.action!r}",
                )
        sqs.delete_message(receipt)
        return

    try:
        params = handler.params_model.model_validate(job.action_params)
    except Exception as exc:
        logger.warning(
            "run_id=%s job_id=%s action=%r: params validation failed; marking FAILED",
            run_id,
            job_id,
            job.action,
        )
        async with session_factory() as session:
            async with session.begin():
                await _write_permanent_failure(
                    session,
                    run_id,
                    job_id,
                    error_message=f"invalid params: {exc}",
                    event_data={"error": str(exc)},
                )
        sqs.delete_message(receipt)
        return

    # Auto-wire from_run_id from wait_for_run_id for recurring chains (issue #202).
    # One-shot chains set from_run_id explicitly at job create time (is None check skips).
    # Non-chained jobs have wait_for_run_id=None (no injection). Only the recurring chain
    # case (from_run_id unset, wait_for_run_id present) needs runtime injection.
    if hasattr(params, "from_run_id") and params.from_run_id is None:
        if run.wait_for_run_id is not None:
            params = params.model_copy(update={"from_run_id": run.wait_for_run_id})
            logger.debug(
                "run_id=%s: auto-wired from_run_id=%s from wait_for_run_id",
                run_id,
                run.wait_for_run_id,
            )

    # Phase 3: dispatch with per-action timeout
    try:
        async with _heartbeat(sqs, receipt, interval=heartbeat_interval):
            action_result = await asyncio.wait_for(
                handler.execute(run, params),
                timeout=handler.timeout_seconds,
            )
    except TimeoutError:
        logger.warning(
            "run_id=%s action=%r timed out after %ds; marking RETRYING",
            run_id,
            job.action,
            handler.timeout_seconds,
        )
        async with session_factory() as session:
            async with session.begin():
                await _write_retrying(
                    session,
                    run_id,
                    job_id,
                    error_message=f"action timed out after {handler.timeout_seconds}s",
                )
        return
    except Exception as exc:
        logger.exception(
            "run_id=%s action=%r raised; marking RETRYING",
            run_id,
            job.action,
        )
        async with session_factory() as session:
            async with session.begin():
                await _write_retrying(
                    session,
                    run_id,
                    job_id,
                    error_message=f"action raised: {exc!r}",
                )
        return

    # Phase 4: write terminal status in one transaction + DeleteMessage
    if action_result.ok:
        terminal_status = "SUCCEEDED"
    elif not action_result.retryable:
        terminal_status = "FAILED"
    else:
        # retryable=True —> RETRYING + RunEvent(RETRY); SQS visibility expiry redelivers.
        logger.info(
            "run_id=%s action=%r returned retryable failure; marking RETRYING",
            run_id,
            job.action,
        )
        async with session_factory() as session:
            async with session.begin():
                await _write_retrying(
                    session,
                    run_id,
                    job_id,
                    error_message=action_result.error or "retryable failure",
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
