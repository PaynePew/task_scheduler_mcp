"""Integration tests for app/workers/executor.py — requires running Postgres + ElasticMQ.

Run with:
    docker compose up -d postgres elasticmq && alembic upgrade head
    uv run pytest -m integration tests/integration/test_executor.py
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.actions.base import ActionResult
from app.actions.registry import ACTION_REGISTRY
from app.db.engine import create_async_engine
from app.db.models import Job, JobRun, RunEvent
from app.queue.sqs import SQSClient
from app.workers.executor import process_one

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def session_factory():
    """Fresh engine per test; cleans all job data on teardown."""
    engine = create_async_engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    async with factory() as session:
        async with session.begin():
            await session.execute(text("DELETE FROM run_events"))
            await session.execute(text("DELETE FROM job_runs"))
            await session.execute(text("DELETE FROM jobs"))
    await engine.dispose()


@pytest.fixture
def sqs() -> SQSClient:
    """SQSClient pointed at ElasticMQ; drains leftover messages from BOTH queues.

    DLQ drain is required because an interrupted prior run of
    test_retryable_three_times_lands_in_dlq can leave stale messages in the DLQ,
    causing the next run's `assert len(dlq_msgs) == 1` to see 2 messages and fail.
    """
    from app.config.settings import settings

    def _drain(c: SQSClient) -> None:
        while True:
            msgs = c.receive_messages(max_messages=10, wait_seconds=0)
            if not msgs:
                break
            for msg in msgs:
                c.delete_message(msg["ReceiptHandle"])

    main = SQSClient()
    _drain(main)
    _drain(SQSClient(queue_url=settings.queue_dlq_url))
    return main


async def _insert_queued_run(
    factory: async_sessionmaker,
    *,
    action: str = "echo",
    action_params: dict | None = None,
    status: str = "QUEUED",
) -> tuple[Job, JobRun]:
    """Insert a Job + JobRun at the given status, committed."""
    if action_params is None:
        action_params = {"message": "hello from integration test"}
    scheduled = datetime.now(tz=UTC) - timedelta(seconds=10)
    async with factory() as session:
        async with session.begin():
            job = Job(
                user_id="executor-test",
                description="test job",
                action=action,
                action_params=action_params,
                job_type="one_shot",
                scheduled_at=scheduled,
            )
            session.add(job)
            await session.flush()

            bucket = scheduled.replace(minute=0, second=0, microsecond=0).isoformat()
            run = JobRun(
                time_bucket=bucket,
                job_id=job.job_id,
                scheduled_at=scheduled,
                status=status,
            )
            session.add(run)
    return job, run


def _make_sqs_message(run_id: int, job_id: int) -> dict:
    return {
        "Body": json.dumps({"run_id": run_id, "job_id": job_id}),
        "ReceiptHandle": f"fake-receipt-{run_id}",
        "MessageId": f"fake-msg-{run_id}",
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_queued_echo_run_completes_to_succeeded(session_factory, sqs):
    """QUEUED echo run → process_one → SUCCEEDED + RunEvent(SUCCEEDED) + DeleteMessage."""
    job, run = await _insert_queued_run(session_factory)
    message = _make_sqs_message(run.run_id, job.job_id)

    # The message is synthetic — we never put it on ElasticMQ — so the fake
    # ReceiptHandle would be rejected if forwarded. The spy only needs to
    # record that process_one called delete_message with the right receipt.
    deleted_receipts: list[str] = []
    sqs.delete_message = deleted_receipts.append

    await process_one(session_factory, sqs, message, registry=ACTION_REGISTRY)

    # DB: run should be SUCCEEDED
    async with session_factory() as session:
        async with session.begin():
            updated_run = (
                await session.execute(select(JobRun).where(JobRun.run_id == run.run_id))
            ).scalar_one()
            events = (
                (await session.execute(select(RunEvent).where(RunEvent.run_id == run.run_id)))
                .scalars()
                .all()
            )

    assert updated_run.status == "SUCCEEDED"
    assert updated_run.start_at is not None
    assert updated_run.finish_at is not None

    event_types = {e.event_type for e in events}
    assert "STARTED" in event_types
    assert "SUCCEEDED" in event_types

    started = next(e for e in events if e.event_type == "STARTED")
    assert started.status_from == "QUEUED"
    assert started.status_to == "RUNNING"

    succeeded = next(e for e in events if e.event_type == "SUCCEEDED")
    assert succeeded.status_from == "RUNNING"
    assert succeeded.status_to == "SUCCEEDED"

    # SQS message was deleted
    assert message["ReceiptHandle"] in deleted_receipts


@pytest.mark.integration
async def test_handler_permanent_failure_marks_run_failed(session_factory, sqs):
    """Handler returning ok=False, retryable=False → FAILED + RunEvent(FAILED) + DeleteMessage."""

    class NoParams(BaseModel):
        pass

    class AlwaysFailHandler:
        name = "always_fail"
        params_model = NoParams
        timeout_seconds = 10

        async def execute(self, run, params) -> ActionResult:
            return ActionResult(ok=False, result=None, error="permanent error", retryable=False)

    job, run = await _insert_queued_run(session_factory, action="always_fail", action_params={})
    message = _make_sqs_message(run.run_id, job.job_id)

    registry = {"always_fail": AlwaysFailHandler()}

    # Synthetic message — fake receipt would be rejected by ElasticMQ if forwarded.
    deleted_receipts: list[str] = []
    sqs.delete_message = deleted_receipts.append

    await process_one(session_factory, sqs, message, registry=registry)

    async with session_factory() as session:
        async with session.begin():
            updated_run = (
                await session.execute(select(JobRun).where(JobRun.run_id == run.run_id))
            ).scalar_one()
            events = (
                (await session.execute(select(RunEvent).where(RunEvent.run_id == run.run_id)))
                .scalars()
                .all()
            )

    assert updated_run.status == "FAILED"
    assert updated_run.error_message == "permanent error"

    event_types = {e.event_type for e in events}
    assert "STARTED" in event_types
    assert "FAILED" in event_types

    assert message["ReceiptHandle"] in deleted_receipts


@pytest.mark.integration
async def test_concurrent_workers_duplicate_message_exactly_one_execution(session_factory, sqs):
    """Two workers receiving the same redelivered message: exactly one executes."""
    job, run = await _insert_queued_run(session_factory)
    message = _make_sqs_message(run.run_id, job.job_id)

    delete_count = 0
    orig_delete = sqs.delete_message

    def _count_delete(receipt):
        nonlocal delete_count
        delete_count += 1
        # Simulate successful delete (ignore "not found" for duplicate receipts)
        try:
            orig_delete(receipt)
        except Exception:
            pass

    sqs.delete_message = _count_delete

    # Run two workers concurrently with the same message (simulating duplicate delivery)
    await asyncio.gather(
        process_one(session_factory, sqs, message, registry=ACTION_REGISTRY),
        process_one(session_factory, sqs, message, registry=ACTION_REGISTRY),
    )

    # DB: exactly one SUCCEEDED event
    async with session_factory() as session:
        async with session.begin():
            updated_run = (
                await session.execute(select(JobRun).where(JobRun.run_id == run.run_id))
            ).scalar_one()
            succeeded_events = (
                (
                    await session.execute(
                        select(RunEvent).where(
                            RunEvent.run_id == run.run_id,
                            RunEvent.event_type == "SUCCEEDED",
                        )
                    )
                )
                .scalars()
                .all()
            )
            started_events = (
                (
                    await session.execute(
                        select(RunEvent).where(
                            RunEvent.run_id == run.run_id,
                            RunEvent.event_type == "STARTED",
                        )
                    )
                )
                .scalars()
                .all()
            )

    assert updated_run.status == "SUCCEEDED"
    assert len(succeeded_events) == 1, "exactly one SUCCEEDED event"
    assert len(started_events) == 1, "exactly one STARTED event"

    # Both workers call delete_message: winner after completion, loser after no-op claim
    assert delete_count == 2


@pytest.mark.integration
async def test_invalid_params_marks_run_failed_and_deletes_message(session_factory, sqs):
    """Echo job with missing 'message' field → FAILED within one cycle; SQS message gone."""
    job, run = await _insert_queued_run(
        session_factory,
        action="echo",
        action_params={},  # missing required "message"
    )
    message = _make_sqs_message(run.run_id, job.job_id)

    deleted_receipts: list[str] = []
    sqs.delete_message = deleted_receipts.append

    await process_one(session_factory, sqs, message, registry=ACTION_REGISTRY)

    async with session_factory() as session:
        async with session.begin():
            updated_run = (
                await session.execute(select(JobRun).where(JobRun.run_id == run.run_id))
            ).scalar_one()
            events = (
                (await session.execute(select(RunEvent).where(RunEvent.run_id == run.run_id)))
                .scalars()
                .all()
            )

    assert updated_run.status == "FAILED"
    assert updated_run.finish_at is not None
    assert updated_run.error_message is not None
    assert "invalid params" in updated_run.error_message

    event_types = {e.event_type for e in events}
    assert "STARTED" in event_types
    assert "FAILED" in event_types

    failed_event = next(e for e in events if e.event_type == "FAILED")
    assert failed_event.status_from == "RUNNING"
    assert failed_event.status_to == "FAILED"
    assert failed_event.event_data is not None

    # SQS message must be deleted — no more redelivery
    assert message["ReceiptHandle"] in deleted_receipts


@pytest.mark.integration
async def test_unknown_action_marks_run_failed_and_deletes_message(session_factory, sqs):
    """Job with unregistered action → FAILED within one cycle; SQS message gone."""
    job, run = await _insert_queued_run(
        session_factory,
        action="nonexistent_action_xyz",
        action_params={"key": "val"},
    )
    message = _make_sqs_message(run.run_id, job.job_id)

    deleted_receipts: list[str] = []
    sqs.delete_message = deleted_receipts.append

    # Use the real ACTION_REGISTRY (which has no "nonexistent_action_xyz")
    await process_one(session_factory, sqs, message, registry=ACTION_REGISTRY)

    async with session_factory() as session:
        async with session.begin():
            updated_run = (
                await session.execute(select(JobRun).where(JobRun.run_id == run.run_id))
            ).scalar_one()
            events = (
                (await session.execute(select(RunEvent).where(RunEvent.run_id == run.run_id)))
                .scalars()
                .all()
            )

    assert updated_run.status == "FAILED"
    assert updated_run.finish_at is not None
    assert updated_run.error_message is not None
    assert "unknown action" in updated_run.error_message

    event_types = {e.event_type for e in events}
    assert "STARTED" in event_types
    assert "FAILED" in event_types

    failed_event = next(e for e in events if e.event_type == "FAILED")
    assert failed_event.status_from == "RUNNING"
    assert failed_event.status_to == "FAILED"

    # SQS message must be deleted — no more redelivery
    assert message["ReceiptHandle"] in deleted_receipts


@pytest.mark.integration
async def test_retryable_failure_marks_retrying_and_does_not_delete(session_factory, sqs):
    """Handler returning ok=False, retryable=True -> RETRYING + no DeleteMessage."""

    class NoParams(BaseModel):
        pass

    class RetryableFailHandler:
        name = "retryable_fail"
        params_model = NoParams
        timeout_seconds = 10

        async def execute(self, run, params) -> ActionResult:
            return ActionResult(ok=False, result=None, error="transient error", retryable=True)

    job, run = await _insert_queued_run(session_factory, action="retryable_fail", action_params={})
    message = _make_sqs_message(run.run_id, job.job_id)

    deleted_receipts: list[str] = []
    sqs.delete_message = deleted_receipts.append

    registry = {"retryable_fail": RetryableFailHandler()}
    await process_one(session_factory, sqs, message, registry=registry)

    async with session_factory() as session:
        async with session.begin():
            updated_run = (
                await session.execute(select(JobRun).where(JobRun.run_id == run.run_id))
            ).scalar_one()
            events = (
                (await session.execute(select(RunEvent).where(RunEvent.run_id == run.run_id)))
                .scalars()
                .all()
            )
    # 1) Run 留在 RETRYING (非終態)
    assert updated_run.status == "RETRYING"
    assert updated_run.finish_at is None, "RETRYING is not terminal - finish_at must stay None"
    assert updated_run.error_message == "transient error"

    # 2) 事件序: STARTED -> RETRY
    event_types = [e.event_type for e in sorted(events, key=lambda e: e.event_id)]
    assert event_types == ["STARTED", "RETRY"]
    retry_event = next(e for e in events if e.event_type == "RETRY")
    assert retry_event.status_from == "RUNNING"
    assert retry_event.status_to == "RETRYING"

    # 3) 訊息沒有被刪 - 留給 SQS visibility expiry 處理 redelivery
    assert deleted_receipts == [], "DeleteMessage must not be called for retryable failure"


@pytest.mark.integration
async def test_action_timeout_marks_retrying_and_does_not_delete(session_factory, sqs):
    """Action exceeding timeout_seconds -> RETRYING, no DeleteMessage."""

    class NoParams(BaseModel):
        pass

    class SlowHandler:
        name = "slow"
        params_model = NoParams
        timeout_seconds = 1

        async def execute(self, run, params) -> ActionResult:
            await asyncio.sleep(5)
            return ActionResult(ok=True, result={}, error=None)

    job, run = await _insert_queued_run(session_factory, action="slow", action_params={})
    message = _make_sqs_message(run.run_id, job.job_id)

    deleted_receipts: list[str] = []
    sqs.delete_message = deleted_receipts.append

    registry = {"slow": SlowHandler()}
    await process_one(session_factory, sqs, message, registry=registry)

    async with session_factory() as session:
        async with session.begin():
            updated_run = (
                await session.execute(select(JobRun).where(JobRun.run_id == run.run_id))
            ).scalar_one()
            events = (
                (await session.execute(select(RunEvent).where(RunEvent.run_id == run.run_id)))
                .scalars()
                .all()
            )

    assert updated_run.status == "RETRYING"
    assert updated_run.finish_at is None
    assert "timed out" in (updated_run.error_message or "")

    event_types = [e.event_type for e in sorted(events, key=lambda e: e.event_id)]
    assert event_types == ["STARTED", "RETRY"]

    assert deleted_receipts == [], "DeleteMessage must not be called on timeout"


@pytest.mark.integration
async def test_action_exception_marks_retrying_and_does_not_delete(session_factory, sqs):
    """Handler raising an unexpected exception -> RETRYING, no DeleteMessage."""

    class NoParams(BaseModel):
        pass

    class BrokenHandler:
        name = "broken"
        params_model = NoParams
        timeout_seconds = 10

        async def execute(self, run, params) -> ActionResult:
            raise RuntimeError("upstream API connection refused")

    job, run = await _insert_queued_run(session_factory, action="broken", action_params={})
    message = _make_sqs_message(run.run_id, job.job_id)

    deleted_receipts: list[str] = []
    sqs.delete_message = deleted_receipts.append

    registry = {"broken": BrokenHandler()}
    await process_one(session_factory, sqs, message, registry=registry)

    async with session_factory() as session:
        async with session.begin():
            updated_run = (
                await session.execute(select(JobRun).where(JobRun.run_id == run.run_id))
            ).scalar_one()

    assert updated_run.status == "RETRYING"
    assert "connection refused" in (updated_run.error_message or "")
    assert deleted_receipts == [], "DeleteMessage must not be called on exception"


@pytest.mark.integration
async def test_heartbeat_extends_visibility_and_cancels_cleanly(session_factory, sqs):
    """Heartbeat fires periodically during action; cancelled cleanly on return; no task leak."""

    class NoParams(BaseModel):
        pass

    class SlowOkHandler:
        name = "slow_ok"
        params_model = NoParams
        timeout_seconds = 10  # 不會 timeout

        async def execute(self, run, params) -> ActionResult:
            await asyncio.sleep(0.3)
            return ActionResult(ok=True, result={}, error=None)

    job, run = await _insert_queued_run(session_factory, action="slow_ok", action_params={})
    message = _make_sqs_message(run.run_id, job.job_id)

    cmv_calls: list[tuple[str, int]] = []

    def _spy_cmv(receipt_handle, *, visibility_timeout):
        cmv_calls.append((receipt_handle, visibility_timeout))

    sqs.change_message_visibility = _spy_cmv
    sqs.delete_message = lambda r: None

    registry = {"slow_ok": SlowOkHandler()}
    tasks_before = asyncio.all_tasks()

    await process_one(
        session_factory,
        sqs,
        message,
        registry=registry,
        heartbeat_interval=0.1,  # 0.3s 動作預期 2~3 次 heartbeat
    )

    # 1) Heartbeat ≥ 2 次
    assert len(cmv_calls) >= 2

    # 2) Receipt + extension 正確
    for receipt, vt in cmv_calls:
        assert receipt == message["ReceiptHandle"]
        assert vt == 60

    # 3) 主路徑沒被影響
    async with session_factory() as session:
        async with session.begin():
            updated_run = (
                await session.execute(select(JobRun).where(JobRun.run_id == run.run_id))
            ).scalar_one()
    assert updated_run.status == "SUCCEEDED"

    # 4) 沒洩漏 — issue #8 HITL 明文要求
    leaked = [t for t in (asyncio.all_tasks() - tasks_before) if not t.done()]
    assert leaked == [], f"heartbeat task leaked: {leaked}"


@pytest.mark.integration
async def test_retryable_three_times_lands_in_dlq(session_factory, sqs):
    """3 次 retryable 後進入 DLQ (end-to-end via ElasticMQ + redrive policy)."""
    from app.config.settings import settings

    class NoParams(BaseModel):
        pass

    class AlwaysRetryHandler:
        name = "always_retry"
        params_model = NoParams
        timeout_seconds = 10

        async def execute(self, run, params) -> ActionResult:
            return ActionResult(ok=False, result=None, error="transient", retryable=True)

    job, run = await _insert_queued_run(session_factory, action="always_retry", action_params={})

    # 真的把 message 丟到 task-queue (非合成)
    sqs.send_message_batch(
        [
            {
                "Id": str(run.run_id),
                "MessageBody": json.dumps({"run_id": run.run_id, "job_id": job.job_id}),
            }
        ]
    )

    registry = {"always_retry": AlwaysRetryHandler()}

    # 連續3次: receive -> process_one(-> RETRYING, 不刪) -> 等 visibility過期
    # SHORT_VISIBILITY=2 + sleep buffer=2.5 gives ElasticMQ enough headroom on
    # contended CI runners; the previous 1s+0.3s buffer was too tight and flaked.
    SHORT_VISIBILITY = 2
    for attempt in range(3):
        msg = sqs.receive_messages(
            max_messages=1,
            wait_seconds=2,
            visibility_timeout=SHORT_VISIBILITY,
        )
        assert len(msg) == 1, f"attempt {attempt + 1}: main queue should have message"
        await process_one(session_factory, sqs, msg[0], registry=registry)
        await asyncio.sleep(SHORT_VISIBILITY + 0.5)  # 等 visibility 過

    # ElasticMQ 把第3+次的 deliver 視為超過 maxReceiveCount=3，redirect 到 DQL
    # 給SQS 一拍 sweep
    await asyncio.sleep(1)

    # 1) Main queue 不該再有這筆
    main_msgs = sqs.receive_messages(max_messages=1, wait_seconds=3)
    assert main_msgs == [], "message should no longer be in main queue"

    # 2) DLQ 應該收到這筆
    dlq_client = SQSClient(queue_url=settings.queue_dlq_url)
    dlq_msgs = dlq_client.receive_messages(max_messages=1, wait_seconds=3)
    assert len(dlq_msgs) == 1, "message should land in DLQ"

    body = json.loads(dlq_msgs[0]["Body"])
    assert body["run_id"] == run.run_id

    # 3) DB 那邊 run 留在 RETRYING (最後一次 process_one 寫的) --非終態
    #    這是個know design - 訊息進DLQ後，DB沒人去把 RETRYING翻 FAILED。
    #    這條留給之後的 reconcile sweep 處理(issue#8 的 follow-up))
    async with session_factory() as session:
        async with session.begin():
            updated_run = (
                await session.execute(select(JobRun).where(JobRun.run_id == run.run_id))
            ).scalar_one()
    assert updated_run.status == "RETRYING"

    for msg in dlq_msgs:
        dlq_client.delete_message(msg["ReceiptHandle"])
