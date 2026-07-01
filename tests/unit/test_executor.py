"""Unit tests for app/workers/executor.py — uses async mocks, no DB or SQS required."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.actions.base import ActionResult
from app.workers.executor import _claim, _write_permanent_failure, _write_terminal, process_one

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _async_cm(yield_value=None) -> MagicMock:
    """Async context manager that yields yield_value."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=yield_value)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _make_message(run_id: int = 1, job_id: int = 1) -> dict:
    return {
        "Body": json.dumps({"run_id": run_id, "job_id": job_id}),
        "ReceiptHandle": f"receipt-{run_id}",
        "MessageId": f"msg-{run_id}",
    }


def _make_job(action: str = "echo", action_params: dict | None = None) -> MagicMock:
    job = MagicMock()
    job.action = action
    job.action_params = action_params or {"message": "hello"}
    return job


def _make_run(run_id: int = 1, job_id: int = 1) -> MagicMock:
    run = MagicMock()
    run.run_id = run_id
    run.job_id = job_id
    run.status = "RUNNING"
    return run


def _make_handler(result: ActionResult) -> MagicMock:
    handler = MagicMock()
    handler.execute = AsyncMock(return_value=result)
    handler.timeout_seconds = 10
    handler.params_model = MagicMock()
    handler.params_model.model_validate = MagicMock(return_value=MagicMock())
    return handler


def _make_session(
    *,
    claim_wins: bool = True,
    prev_status: str = "QUEUED",
    job: MagicMock | None = None,
    run: MagicMock | None = None,
) -> MagicMock:
    """Build a mock session that satisfies the four ``session.execute()`` calls
    a full ``process_one`` invocation issues against a single shared session mock.

    Slot 1 — ``_claim``'s prev_status ``SELECT``: drives ``RunEvent(STARTED).status_from``.
    Slot 2 — ``_claim``'s ``UPDATE ... RETURNING``: ``.first()`` drives ``claim_wins``.
    Slot 3 — ``process_one``'s run-load ``SELECT``: ``.scalar_one_or_none()`` returns ``run``.
    Beyond slot 3 — terminal / retrying / permanent-failure ``UPDATE`` plus the
    ``settle_job`` CAS that ``_write_terminal`` / ``_write_permanent_failure`` now
    issue (ADR-068). These get a generic result so the number of trailing writes
    doesn't matter to the mock.
    """
    prev_status_result = MagicMock()
    prev_status_result.scalar_one_or_none.return_value = prev_status

    claim_result = MagicMock()
    claim_result.first.return_value = MagicMock() if claim_wins else None

    run_result = MagicMock()
    run_result.scalar_one_or_none.return_value = run

    ordered = [prev_status_result, claim_result, run_result]

    def _execute(*_args, **_kwargs) -> MagicMock:
        # Return the ordered slots first, then a generic result for any trailing
        # write (terminal UPDATE, settle CAS, ...).
        return ordered.pop(0) if ordered else MagicMock()

    session = MagicMock()
    session.execute = AsyncMock(side_effect=_execute)
    session.get = AsyncMock(return_value=job)
    session.add = MagicMock()
    session.begin = MagicMock(return_value=_async_cm(None))
    return session


def _make_factory(session: MagicMock) -> MagicMock:
    """Factory always returns the same session (fine for unit tests)."""
    return MagicMock(return_value=_async_cm(session))


# ---------------------------------------------------------------------------
# _claim unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_returns_true_when_row_returned():
    """UPDATE matches a row → claim succeeds."""
    mock_result = MagicMock()
    mock_result.first.return_value = MagicMock(run_id=1, job_id=1)

    session = MagicMock()
    session.execute = AsyncMock(return_value=mock_result)
    session.add = MagicMock()

    claimed = await _claim(session, run_id=1, job_id=1)

    assert claimed is True
    session.add.assert_called_once()  # RunEvent(STARTED) inserted


@pytest.mark.asyncio
async def test_claim_sets_heartbeat_at():
    """The RUNNING claim UPDATE sets heartbeat_at (issue #267 DB-side lease)."""
    mock_result = MagicMock()
    mock_result.first.return_value = MagicMock(run_id=1, job_id=1)

    session = MagicMock()
    session.execute = AsyncMock(return_value=mock_result)
    session.add = MagicMock()

    await _claim(session, run_id=1, job_id=1)

    update_stmt = session.execute.await_args_list[-1].args[0]
    set_columns = {col.name for col in update_stmt._values}
    assert "heartbeat_at" in set_columns


@pytest.mark.asyncio
async def test_claim_returns_false_when_no_row():
    """UPDATE matches 0 rows (already claimed) → returns False, no RunEvent."""
    mock_result = MagicMock()
    mock_result.first.return_value = None

    session = MagicMock()
    session.execute = AsyncMock(return_value=mock_result)
    session.add = MagicMock()

    claimed = await _claim(session, run_id=1, job_id=1)

    assert claimed is False
    session.add.assert_not_called()


# ---------------------------------------------------------------------------
# _write_terminal unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_terminal_succeeded():
    """SUCCEEDED: updates status column and inserts RunEvent."""
    session = MagicMock()
    session.execute = AsyncMock(return_value=MagicMock())
    session.add = MagicMock()

    result = ActionResult(ok=True, result={"echoed": "hi"}, error=None)
    await _write_terminal(
        session, run_id=1, job_id=1, terminal_status="SUCCEEDED", action_result=result
    )

    # Two executes: the terminal status UPDATE + the settle_job CAS (ADR-068).
    assert session.execute.await_count == 2
    session.add.assert_called_once()
    added = session.add.call_args[0][0]
    assert added.event_type == "SUCCEEDED"
    assert added.status_from == "RUNNING"
    assert added.status_to == "SUCCEEDED"


@pytest.mark.asyncio
async def test_write_terminal_failed():
    """FAILED: inserts RunEvent with FAILED event_type."""
    session = MagicMock()
    session.execute = AsyncMock(return_value=MagicMock())
    session.add = MagicMock()

    result = ActionResult(ok=False, result=None, error="oops", retryable=False)
    await _write_terminal(
        session, run_id=2, job_id=2, terminal_status="FAILED", action_result=result
    )

    added = session.add.call_args[0][0]
    assert added.event_type == "FAILED"


# ---------------------------------------------------------------------------
# process_one — claim-failure path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_one_no_claim_deletes_message():
    """When another worker already claimed the run, DeleteMessage and return."""
    session = _make_session(claim_wins=False)
    factory = _make_factory(session)
    sqs = MagicMock()

    await process_one(factory, sqs, _make_message(), registry={})

    sqs.delete_message.assert_called_once_with("receipt-1")


@pytest.mark.asyncio
async def test_process_one_no_claim_does_not_write_terminal():
    """No claim → no terminal write."""
    session = _make_session(claim_wins=False)
    factory = _make_factory(session)
    sqs = MagicMock()

    await process_one(factory, sqs, _make_message(), registry={})

    # _claim issues 2 executes (prev_status SELECT + UPDATE...RETURNING); after the
    # UPDATE matches 0 rows, process_one short-circuits before the run-load SELECT.
    assert session.execute.await_count == 2


# ---------------------------------------------------------------------------
# process_one — SUCCEEDED path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_one_succeeded_deletes_message():
    """ok=True → SUCCEEDED written, DeleteMessage called."""
    job = _make_job()
    run = _make_run()
    session = _make_session(claim_wins=True, job=job, run=run)
    factory = _make_factory(session)
    sqs = MagicMock()

    handler = _make_handler(ActionResult(ok=True, result={"echoed": "hello"}, error=None))
    registry = {"echo": handler}

    await process_one(factory, sqs, _make_message(), registry=registry)

    sqs.delete_message.assert_called_once_with("receipt-1")
    handler.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_process_one_succeeded_writes_terminal_event():
    """ok=True → terminal session writes SUCCEEDED RunEvent."""
    job = _make_job()
    run = _make_run()
    session = _make_session(claim_wins=True, job=job, run=run)
    factory = _make_factory(session)
    sqs = MagicMock()

    handler = _make_handler(ActionResult(ok=True, result={"echoed": "hello"}, error=None))
    registry = {"echo": handler}

    await process_one(factory, sqs, _make_message(), registry=registry)

    # session.add should be called twice: once for RunEvent(STARTED), once for RunEvent(SUCCEEDED)
    assert session.add.call_count == 2
    events = [c[0][0] for c in session.add.call_args_list]
    event_types = {e.event_type for e in events}
    assert "STARTED" in event_types
    assert "SUCCEEDED" in event_types


# ---------------------------------------------------------------------------
# process_one — FAILED path (retryable=False)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_one_permanent_failure_deletes_message():
    """ok=False, retryable=False → FAILED written, DeleteMessage called."""
    job = _make_job()
    run = _make_run()
    session = _make_session(claim_wins=True, job=job, run=run)
    factory = _make_factory(session)
    sqs = MagicMock()

    handler = _make_handler(ActionResult(ok=False, result=None, error="bad input", retryable=False))
    registry = {"echo": handler}

    await process_one(factory, sqs, _make_message(), registry=registry)

    sqs.delete_message.assert_called_once_with("receipt-1")
    events = [c[0][0] for c in session.add.call_args_list]
    event_types = {e.event_type for e in events}
    assert "FAILED" in event_types


# ---------------------------------------------------------------------------
# process_one — retryable=True path (S07a: leave message)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_one_retryable_leaves_message():
    """ok=False, retryable=True → do NOT DeleteMessage (S07b handles retry)."""
    job = _make_job()
    run = _make_run()
    # 4 slots: prev_status, claim_update, run_load, _write_retrying's UPDATE.
    session = _make_session(claim_wins=True, job=job, run=run)
    factory = _make_factory(session)
    sqs = MagicMock()

    handler = _make_handler(ActionResult(ok=False, result=None, error="transient", retryable=True))
    registry = {"echo": handler}

    await process_one(factory, sqs, _make_message(), registry=registry)

    sqs.delete_message.assert_not_called()


# ---------------------------------------------------------------------------
# process_one — handler exception (S07a: leave message)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_one_handler_exception_leaves_message():
    """Handler raises → do NOT DeleteMessage (SQS visibility expiry handles redelivery)."""
    job = _make_job()
    run = _make_run()
    # 4 slots: prev_status, claim_update, run_load, _write_retrying's UPDATE.
    session = _make_session(claim_wins=True, job=job, run=run)
    factory = _make_factory(session)
    sqs = MagicMock()

    broken_handler = MagicMock()
    broken_handler.execute = AsyncMock(side_effect=RuntimeError("boom"))
    broken_handler.timeout_seconds = 10
    broken_handler.params_model = MagicMock()
    broken_handler.params_model.model_validate = MagicMock(return_value=MagicMock())
    registry = {"echo": broken_handler}

    await process_one(factory, sqs, _make_message(), registry=registry)

    sqs.delete_message.assert_not_called()


# ---------------------------------------------------------------------------
# process_one — timeout (S07a: leave message)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_one_handler_timeout_leaves_message():
    """Handler times out → do NOT DeleteMessage."""
    job = _make_job()
    run = _make_run()
    # 4 slots: prev_status, claim_update, run_load, _write_retrying's UPDATE.
    session = _make_session(claim_wins=True, job=job, run=run)
    factory = _make_factory(session)
    sqs = MagicMock()

    async def _slow(*_a, **_kw):
        await asyncio.sleep(100)

    slow_handler = MagicMock()
    slow_handler.execute = _slow
    slow_handler.timeout_seconds = 0.01  # very short timeout to trigger TimeoutError
    slow_handler.params_model = MagicMock()
    slow_handler.params_model.model_validate = MagicMock(return_value=MagicMock())
    registry = {"echo": slow_handler}

    await process_one(factory, sqs, _make_message(), registry=registry)

    sqs.delete_message.assert_not_called()


# ---------------------------------------------------------------------------
# process_one — duplicate delivery (two workers, same message)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_one_duplicate_delivery_exactly_one_wins():
    """Two concurrent process_one calls on the same message: exactly one claims.

    Winner factory claims successfully; loser factory returns no-row (already claimed).
    Each gets its own session to avoid interleaving mock call-order issues.
    """
    job = _make_job()
    run = _make_run()

    winner_session = _make_session(claim_wins=True, job=job, run=run)
    loser_session = _make_session(claim_wins=False)

    winner_factory = _make_factory(winner_session)
    loser_factory = _make_factory(loser_session)

    sqs = MagicMock()
    handler = _make_handler(ActionResult(ok=True, result={"echoed": "hi"}, error=None))
    registry = {"echo": handler}

    msg = _make_message()
    await asyncio.gather(
        process_one(winner_factory, sqs, msg, registry=registry),
        process_one(loser_factory, sqs, msg, registry=registry),
    )

    # handler executed exactly once (by the winner)
    assert handler.execute.await_count == 1
    # delete_message called twice: winner completes + loser no-ops
    assert sqs.delete_message.call_count == 2


# ---------------------------------------------------------------------------
# _write_permanent_failure unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_permanent_failure_updates_run_and_inserts_event():
    """_write_permanent_failure writes FAILED status and a FAILED RunEvent."""
    session = MagicMock()
    session.execute = AsyncMock(return_value=MagicMock())
    session.add = MagicMock()

    await _write_permanent_failure(
        session,
        run_id=7,
        job_id=3,
        error_message="invalid params: missing field",
        event_data={"error": "missing field"},
    )

    # Two executes: the FAILED status UPDATE + the settle_job CAS (ADR-068).
    assert session.execute.await_count == 2
    session.add.assert_called_once()
    added = session.add.call_args[0][0]
    assert added.event_type == "FAILED"
    assert added.status_from == "RUNNING"
    assert added.status_to == "FAILED"
    assert added.event_data == {"error": "missing field"}


# ---------------------------------------------------------------------------
# process_one — job/run row missing after claim (permanent failure)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_one_job_not_found_deletes_message():
    """Job row missing after claim → DeleteMessage, no terminal write."""
    run = _make_run()
    session = _make_session(claim_wins=True, job=None, run=run)
    factory = _make_factory(session)
    sqs = MagicMock()

    await process_one(factory, sqs, _make_message(), registry={"echo": MagicMock()})

    sqs.delete_message.assert_called_once_with("receipt-1")
    # Only RunEvent(STARTED) written — no FAILED since row is gone
    assert session.add.call_count == 1
    assert session.add.call_args[0][0].event_type == "STARTED"


@pytest.mark.asyncio
async def test_process_one_run_not_found_deletes_message():
    """Run row missing after claim → DeleteMessage, no terminal write."""
    job = _make_job()
    session = _make_session(claim_wins=True, job=job, run=None)
    factory = _make_factory(session)
    sqs = MagicMock()

    await process_one(factory, sqs, _make_message(), registry={"echo": MagicMock()})

    sqs.delete_message.assert_called_once_with("receipt-1")
    assert session.add.call_count == 1
    assert session.add.call_args[0][0].event_type == "STARTED"


# ---------------------------------------------------------------------------
# process_one — unknown action (permanent failure → FAILED + DeleteMessage)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_one_unknown_action_deletes_message():
    """Unknown action → DeleteMessage."""
    job = _make_job(action="nonexistent")
    run = _make_run()
    session = _make_session(claim_wins=True, job=job, run=run)
    factory = _make_factory(session)
    sqs = MagicMock()

    await process_one(factory, sqs, _make_message(), registry={})

    sqs.delete_message.assert_called_once_with("receipt-1")


@pytest.mark.asyncio
async def test_process_one_unknown_action_writes_failed_event():
    """Unknown action → FAILED RunEvent written."""
    job = _make_job(action="nonexistent")
    run = _make_run()
    session = _make_session(claim_wins=True, job=job, run=run)
    factory = _make_factory(session)
    sqs = MagicMock()

    await process_one(factory, sqs, _make_message(), registry={})

    # session.add: RunEvent(STARTED) + RunEvent(FAILED)
    assert session.add.call_count == 2
    events = [c[0][0] for c in session.add.call_args_list]
    event_types = {e.event_type for e in events}
    assert "STARTED" in event_types
    assert "FAILED" in event_types

    failed_event = next(e for e in events if e.event_type == "FAILED")
    assert failed_event.status_from == "RUNNING"
    assert failed_event.status_to == "FAILED"


@pytest.mark.asyncio
async def test_process_one_unknown_action_does_not_call_handler():
    """Unknown action → handler is never invoked."""
    job = _make_job(action="nonexistent")
    run = _make_run()
    session = _make_session(claim_wins=True, job=job, run=run)
    factory = _make_factory(session)
    sqs = MagicMock()
    handler = _make_handler(ActionResult(ok=True, result={}, error=None))

    # "echo" handler registered but job uses "nonexistent" — should never execute
    await process_one(factory, sqs, _make_message(), registry={"echo": handler})

    handler.execute.assert_not_awaited()


# ---------------------------------------------------------------------------
# process_one — invalid params (permanent failure → FAILED + DeleteMessage)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_one_invalid_params_deletes_message():
    """Params validation failure → DeleteMessage."""
    job = _make_job(action="echo", action_params={})  # missing "message"
    run = _make_run()
    session = _make_session(claim_wins=True, job=job, run=run)
    factory = _make_factory(session)
    sqs = MagicMock()

    broken_handler = MagicMock()
    broken_handler.params_model = MagicMock()
    broken_handler.params_model.model_validate = MagicMock(
        side_effect=ValueError("missing required field")
    )
    registry = {"echo": broken_handler}

    await process_one(factory, sqs, _make_message(), registry=registry)

    sqs.delete_message.assert_called_once_with("receipt-1")


@pytest.mark.asyncio
async def test_process_one_invalid_params_writes_failed_event():
    """Params validation failure → FAILED RunEvent with error in event_data."""
    job = _make_job(action="echo", action_params={})
    run = _make_run()
    session = _make_session(claim_wins=True, job=job, run=run)
    factory = _make_factory(session)
    sqs = MagicMock()

    broken_handler = MagicMock()
    broken_handler.params_model = MagicMock()
    broken_handler.params_model.model_validate = MagicMock(
        side_effect=ValueError("missing required field")
    )
    registry = {"echo": broken_handler}

    await process_one(factory, sqs, _make_message(), registry=registry)

    assert session.add.call_count == 2
    events = [c[0][0] for c in session.add.call_args_list]
    failed_event = next(e for e in events if e.event_type == "FAILED")
    assert failed_event.status_from == "RUNNING"
    assert failed_event.status_to == "FAILED"
    assert failed_event.event_data is not None
    assert "error" in failed_event.event_data


@pytest.mark.asyncio
async def test_process_one_invalid_params_error_message_contains_detail():
    """Params validation failure → error_message in job_runs includes the exception text."""
    job = _make_job(action="echo", action_params={})
    run = _make_run()
    # 4 slots: prev_status, claim_update, run_load, _write_permanent_failure's UPDATE.
    session = _make_session(claim_wins=True, job=job, run=run)
    factory = _make_factory(session)
    sqs = MagicMock()

    broken_handler = MagicMock()
    broken_handler.params_model = MagicMock()
    broken_handler.params_model.model_validate = MagicMock(
        side_effect=ValueError("missing required field")
    )
    registry = {"echo": broken_handler}

    await process_one(factory, sqs, _make_message(), registry=registry)

    # Verify the FAILED event's error info is set via session.add
    failed_event = next(
        e for e in [c[0][0] for c in session.add.call_args_list] if e.event_type == "FAILED"
    )
    assert "missing required field" in str(failed_event.event_data)


# ---------------------------------------------------------------------------
# process_one — handler exception still leaves message (S07a regression guard)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_one_handler_exception_still_leaves_message_after_fix():
    """S07a regression: handler exception must still leave message (not FAILED)."""
    job = _make_job()
    run = _make_run()
    # 4 slots: prev_status, claim_update, run_load, _write_retrying's UPDATE.
    session = _make_session(claim_wins=True, job=job, run=run)
    factory = _make_factory(session)
    sqs = MagicMock()

    broken_handler = MagicMock()
    broken_handler.execute = AsyncMock(side_effect=RuntimeError("transient network failure"))
    broken_handler.timeout_seconds = 10
    broken_handler.params_model = MagicMock()
    broken_handler.params_model.model_validate = MagicMock(return_value=MagicMock())
    registry = {"echo": broken_handler}

    await process_one(factory, sqs, _make_message(), registry=registry)

    # Message must NOT be deleted — S07b will handle retry
    sqs.delete_message.assert_not_called()
