"""Integration test: recurring chain re-fires on every upstream tick (ADR-065, issue #224).

Exercises the real materialize → arm → ChainWatcher flip → executor inject path.
NO faked downstream-run creation (the _insert_downstream_run seam is gone).

Topology:
  Job A: recurring (cron "@hourly") — the root; produces a result per tick.
  Job B: trigger-driven (trigger_on_job_id=A, trigger_on_status="ANY") — NO cron.
         (B recurs because A recurs; it must NOT carry its own cron_expr.)

Per-tick flow:
  1. Mark A's current run SUCCEEDED + emit RunEvent(SUCCEEDED).
  2. RecurringJobWatcher.poll_once: sees terminal event on A, calls materialize_successor
     which spawns a new PENDING A run AND arms a new WAITING B run (same transaction).
  3. ChainWatcher.poll_once: flips B's WAITING→PENDING (trigger_on_status="ANY" matches).
  4. executor.process_one(B_run): captures params.from_run_id from wait_for_run_id.
  5. Assert B_run.result["received_from_run_id"] == that tick's A run_id.

Regression assertions:
  - One-shot chains: explicit from_run_id in action_params is NOT overwritten.
  - Non-chained recurring: wait_for_run_id=None → no injection, from_run_id stays None.

Run with:
    uv run pytest -m integration tests/integration/test_chain_recurring.py
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from pydantic import BaseModel
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.actions.base import ActionResult
from app.db.engine import create_async_engine
from app.db.models import Job, JobRun, RunEvent
from app.queue.sqs import SQSClient
from app.workers.chain_watcher import poll_once as chain_poll_once
from app.workers.executor import process_one
from app.workers.recurring_watcher import poll_once as recurring_poll_once

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
    """SQSClient with delete_message stubbed out (messages are synthetic)."""
    client = SQSClient()
    client.delete_message = lambda receipt: None
    return client


def _sqs_message(run_id: int, job_id: int) -> dict:
    return {
        "Body": json.dumps({"run_id": run_id, "job_id": job_id}),
        "ReceiptHandle": f"fake-receipt-{run_id}",
        "MessageId": f"fake-msg-{run_id}",
    }


# ---------------------------------------------------------------------------
# Test handler: captures params.from_run_id at execute() time
# ---------------------------------------------------------------------------


class _CaptureParams(BaseModel):
    from_run_id: int | None = None


class _CapturingHandler:
    """Minimal handler that records the from_run_id it received and stores it in result."""

    name = "capturing_chain"
    params_model = _CaptureParams
    timeout_seconds = 10
    requires_operator = False

    async def execute(self, run: JobRun, params: _CaptureParams) -> ActionResult:
        return ActionResult(
            ok=True,
            result={"received_from_run_id": params.from_run_id},
            error=None,
        )


_REGISTRY = {"capturing_chain": _CapturingHandler()}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _complete_run(
    factory: async_sessionmaker,
    run: JobRun,
    result: dict,
) -> RunEvent:
    """Mark a run SUCCEEDED + emit RunEvent(SUCCEEDED). Returns the committed event."""
    now = datetime.now(tz=UTC)
    async with factory() as session:
        async with session.begin():
            await session.execute(
                update(JobRun)
                .where(JobRun.run_id == run.run_id)
                .values(status="SUCCEEDED", finish_at=now, result=json.dumps(result))
            )
            event = RunEvent(
                run_id=run.run_id,
                job_id=run.job_id,
                event_type="SUCCEEDED",
                status_from="RUNNING",
                status_to="SUCCEEDED",
                occurred_at=now,
            )
            session.add(event)
            await session.flush()
            event_id = event.event_id
    async with factory() as session:
        async with session.begin():
            return (
                await session.execute(select(RunEvent).where(RunEvent.event_id == event_id))
            ).scalar_one()


async def _get_runs_by_status(
    factory: async_sessionmaker, job_id: int, status: str
) -> list[JobRun]:
    async with factory() as session:
        async with session.begin():
            return (
                (
                    await session.execute(
                        select(JobRun).where(
                            JobRun.job_id == job_id,
                            JobRun.status == status,
                        )
                    )
                )
                .scalars()
                .all()
            )


# ---------------------------------------------------------------------------
# Core test: 3 ticks — each B run reads its own tick's A run_id
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_recurring_chain_refires_per_tick_3_ticks(session_factory, sqs):
    """Recurring A (cron) → chained B (NO cron): B refires on every A tick.

    Each B run must read its own tick's A run_id (not a stale first-tick run_id).
    Uses the REAL materialize → arm → ChainWatcher flip → executor inject path.
    B is constructed WITHOUT a cron_expr (inherited recurrence via trigger).
    """
    # -----------------------------------------------------------------------
    # Setup: create Job A (recurring) and Job B (trigger-driven, no cron)
    # Job B has NO cron_expr; it recurs because A recurs.
    # -----------------------------------------------------------------------
    now = datetime.now(tz=UTC)
    initial_bucket = now.replace(minute=0, second=0, microsecond=0).isoformat()

    async with session_factory() as session:
        async with session.begin():
            job_a = Job(
                user_id="chain-recurring-test",
                description="upstream recurring A",
                action="capturing_chain",
                action_params={},
                job_type="recurring",
                cron_expr="@hourly",
                timezone="UTC",
            )
            session.add(job_a)
            await session.flush()

            # A's initial run (PENDING — will be completed per tick)
            run_a0 = JobRun(
                time_bucket=initial_bucket,
                job_id=job_a.job_id,
                user_id=job_a.user_id,
                scheduled_at=now,
                status="PENDING",
            )
            session.add(run_a0)
            await session.flush()
            session.add(
                RunEvent(
                    run_id=run_a0.run_id,
                    job_id=job_a.job_id,
                    event_type="CREATED",
                    status_from=None,
                    status_to="PENDING",
                )
            )

            # B is trigger-driven: NO cron_expr; it recurs because A recurs.
            # job_type=one_shot because it has no cron. It requires scheduled_at.
            job_b = Job(
                user_id="chain-recurring-test",
                description="downstream trigger-driven B (no cron)",
                action="capturing_chain",
                action_params={},
                job_type="one_shot",
                scheduled_at=now,  # required by DB constraint for one_shot
                trigger_on_job_id=job_a.job_id,
                trigger_on_status="ANY",
            )
            session.add(job_b)
            await session.flush()

            # B's initial WAITING run armed against A's first run.
            # This is what materialize_initial (called from create_job) would create.
            run_b0 = JobRun(
                time_bucket=initial_bucket,
                job_id=job_b.job_id,
                user_id=job_b.user_id,
                scheduled_at=now,
                status="WAITING",
                wait_for_run_id=run_a0.run_id,
            )
            session.add(run_b0)
            await session.flush()
            session.add(
                RunEvent(
                    run_id=run_b0.run_id,
                    job_id=job_b.job_id,
                    event_type="CREATED",
                    status_from=None,
                    status_to="WAITING",
                )
            )

    # -----------------------------------------------------------------------
    # Drive 3 ticks
    # -----------------------------------------------------------------------
    a_run_ids_per_tick = []
    b_run_ids_per_tick = []

    a_current = run_a0
    b_current_waiting = run_b0

    for tick in range(3):
        # Step 1: complete A's current run
        await _complete_run(
            session_factory,
            a_current,
            result={"tick": tick, "data": f"tick-{tick}"},
        )
        a_run_ids_per_tick.append(a_current.run_id)

        # Step 2: chain_watcher flips current B WAITING → PENDING
        cw_count = await chain_poll_once(session_factory)
        assert cw_count >= 1, f"tick {tick}: chain_watcher processed 0 events"

        # Verify B's current run is now PENDING
        b_pending_runs = await _get_runs_by_status(session_factory, job_b.job_id, "PENDING")
        matching = [r for r in b_pending_runs if r.run_id == b_current_waiting.run_id]
        assert len(matching) == 1, (
            f"tick {tick}: expected run_id={b_current_waiting.run_id} to be PENDING,"
            f" pending runs: {[r.run_id for r in b_pending_runs]}"
        )
        b_run_ids_per_tick.append(b_current_waiting.run_id)

        # Step 3: execute B's run; capturing handler stores from_run_id in result
        msg = _sqs_message(b_current_waiting.run_id, job_b.job_id)
        await process_one(session_factory, sqs, msg, registry=_REGISTRY)

        # Step 4: recurring_watcher sees A's terminal event
        #         → materialize_successor spawns A(next) + arms B(next) atomically
        rw_count = await recurring_poll_once(session_factory)
        assert rw_count >= 1, f"tick {tick}: recurring_watcher processed 0 events"

        if tick < 2:
            # Verify A has a new PENDING run
            a_pending = await _get_runs_by_status(session_factory, job_a.job_id, "PENDING")
            assert len(a_pending) == 1, (
                f"tick {tick}: expected 1 new PENDING A run, got {len(a_pending)}"
            )
            # Verify B has a new WAITING run (created by _arm inside materialize_successor)
            b_waiting = await _get_runs_by_status(session_factory, job_b.job_id, "WAITING")
            assert len(b_waiting) == 1, (
                f"tick {tick}: expected 1 new WAITING B run (from _arm), got {len(b_waiting)}. "
                "This is the anti-pattern this test was written to catch — _arm must create it."
            )
            a_current = a_pending[0]
            b_current_waiting = b_waiting[0]
            # KEY assertion: each new B WAITING run points at the new A run (not stale)
            assert b_current_waiting.wait_for_run_id == a_current.run_id, (
                f"tick {tick}: B's wait_for_run_id={b_current_waiting.wait_for_run_id}"
                f" != new A run_id={a_current.run_id}. "
                "Arming must be atomic with the upstream run insert."
            )

    # -----------------------------------------------------------------------
    # Final assertion: each B run received its own tick's A run_id
    # -----------------------------------------------------------------------
    async with session_factory() as session:
        async with session.begin():
            b_succeeded = (
                (
                    await session.execute(
                        select(JobRun).where(
                            JobRun.job_id == job_b.job_id,
                            JobRun.status == "SUCCEEDED",
                        )
                    )
                )
                .scalars()
                .all()
            )

    assert len(b_succeeded) == 3, f"expected 3 SUCCEEDED B runs, got {len(b_succeeded)}"

    by_run_id = {r.run_id: json.loads(r.result) for r in b_succeeded}
    for tick in range(3):
        b_run_id = b_run_ids_per_tick[tick]
        expected_a_id = a_run_ids_per_tick[tick]
        received = by_run_id[b_run_id]["received_from_run_id"]
        assert received == expected_a_id, (
            f"tick {tick}: B run_id={b_run_id} received from_run_id={received},"
            f" expected {expected_a_id} (this tick's A run). "
            "Each B tick must read ITS OWN upstream A run_id."
        )


# ---------------------------------------------------------------------------
# One-shot chain regression: explicit from_run_id must NOT be overwritten
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_one_shot_chain_explicit_from_run_id_not_overwritten(session_factory, sqs):
    """One-shot chain: when from_run_id is set at job create time, executor must not override it.

    This ensures the fix only applies when from_run_id is None (unset) — not when
    the caller already hard-coded a specific upstream run_id.
    """
    async with session_factory() as session:
        async with session.begin():
            job_a = Job(
                user_id="one-shot-chain-test",
                description="upstream one-shot",
                action="capturing_chain",
                action_params={},
                job_type="one_shot",
                scheduled_at=datetime.now(tz=UTC) - timedelta(hours=2),
            )
            session.add(job_a)
            await session.flush()

            # Upstream run that the downstream was wired to at create time
            bucket = (
                (datetime.now(tz=UTC) - timedelta(hours=2))
                .replace(minute=0, second=0, microsecond=0)
                .isoformat()
            )
            run_a_original = JobRun(
                time_bucket=bucket,
                job_id=job_a.job_id,
                user_id=job_a.user_id,
                scheduled_at=datetime.now(tz=UTC) - timedelta(hours=2),
                status="SUCCEEDED",
                finish_at=datetime.now(tz=UTC),
                result=json.dumps({"tick": "original"}),
            )
            session.add(run_a_original)
            await session.flush()

            # A later A run — this is what wait_for_run_id would point to if recurring
            run_a_later = JobRun(
                time_bucket=bucket,
                job_id=job_a.job_id,
                user_id=job_a.user_id,
                scheduled_at=datetime.now(tz=UTC) - timedelta(hours=1),
                status="SUCCEEDED",
                finish_at=datetime.now(tz=UTC),
                result=json.dumps({"tick": "later"}),
            )
            session.add(run_a_later)
            await session.flush()

            # B job: from_run_id explicitly set to run_a_original (one-shot wiring)
            job_b = Job(
                user_id="one-shot-chain-test",
                description="downstream one-shot with explicit from_run_id",
                action="capturing_chain",
                action_params={"from_run_id": run_a_original.run_id},
                job_type="one_shot",
                scheduled_at=datetime.now(tz=UTC) - timedelta(minutes=30),
            )
            session.add(job_b)
            await session.flush()

            # B's run — wait_for_run_id points to the LATER A run (as if chain flip occurred)
            # but from_run_id is already set in action_params → should NOT be overwritten
            bucket_b = (
                (datetime.now(tz=UTC) - timedelta(minutes=30))
                .replace(minute=0, second=0, microsecond=0)
                .isoformat()
            )
            run_b = JobRun(
                time_bucket=bucket_b,
                job_id=job_b.job_id,
                user_id=job_b.user_id,
                scheduled_at=datetime.now(tz=UTC) - timedelta(minutes=30),
                status="PENDING",
                wait_for_run_id=run_a_later.run_id,  # different from action_params from_run_id
            )
            session.add(run_b)

    msg = _sqs_message(run_b.run_id, job_b.job_id)
    await process_one(session_factory, sqs, msg, registry=_REGISTRY)

    async with session_factory() as session:
        async with session.begin():
            updated_run = (
                await session.execute(select(JobRun).where(JobRun.run_id == run_b.run_id))
            ).scalar_one()

    assert updated_run.status == "SUCCEEDED"
    result = json.loads(updated_run.result)
    # Must have used the ORIGINAL from_run_id, not the wait_for_run_id
    assert result["received_from_run_id"] == run_a_original.run_id, (
        f"expected original from_run_id={run_a_original.run_id}, "
        f"got {result['received_from_run_id']} (wait_for_run_id={run_a_later.run_id})"
    )


# ---------------------------------------------------------------------------
# Non-chained recurring job: wait_for_run_id=None → no injection
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_non_chained_job_no_from_run_id_injection(session_factory, sqs):
    """Non-chained job (wait_for_run_id=None): no injection; from_run_id stays None."""
    async with session_factory() as session:
        async with session.begin():
            job = Job(
                user_id="non-chained-test",
                description="standalone recurring",
                action="capturing_chain",
                action_params={},  # from_run_id absent
                job_type="recurring",
                cron_expr="@hourly",
                timezone="UTC",
            )
            session.add(job)
            await session.flush()

            bucket = (
                (datetime.now(tz=UTC) - timedelta(hours=1))
                .replace(minute=0, second=0, microsecond=0)
                .isoformat()
            )
            run = JobRun(
                time_bucket=bucket,
                job_id=job.job_id,
                user_id=job.user_id,
                scheduled_at=datetime.now(tz=UTC) - timedelta(hours=1),
                status="PENDING",
                wait_for_run_id=None,  # no upstream dependency
            )
            session.add(run)

    msg = _sqs_message(run.run_id, job.job_id)
    await process_one(session_factory, sqs, msg, registry=_REGISTRY)

    async with session_factory() as session:
        async with session.begin():
            updated_run = (
                await session.execute(select(JobRun).where(JobRun.run_id == run.run_id))
            ).scalar_one()

    assert updated_run.status == "SUCCEEDED"
    result = json.loads(updated_run.result)
    assert result["received_from_run_id"] is None, (
        f"expected no injection (None), got {result['received_from_run_id']}"
    )


# ---------------------------------------------------------------------------
# S2 / issue #226: full fan-out digest shape over multiple ticks
# ---------------------------------------------------------------------------
#
# Topology mirrors the canonical daily-digest workflow from ADR-065:
#
#   root (job_id A, cron "@hourly") — the recurring source
#    └─ mid  (job_id B, trigger_on_job_id=A, no cron) — e.g. llm_summarize
#        ├─ sink1 (job_id C, trigger_on_job_id=B, no cron) — e.g. slack_post
#        └─ sink2 (job_id D, trigger_on_job_id=B, no cron) — e.g. email_send
#
# Per-tick contract:
#   1. Complete root's current run (SUCCEEDED).
#   2. recurring_watcher: materialize_successor → PENDING root(next) + WAITING mid(next).
#      _arm cascades: mid WAITING → arm sink1 WAITING + sink2 WAITING (same transaction).
#   3. chain_watcher: flips mid WAITING → PENDING; then flips sink1/sink2 (after mid done).
#   4. Execute mid; execute sink1 and sink2.
#   5. Each run reads ITS OWN upstream result (mid reads this tick's root; sinks read mid).
#
# Three ticks are exercised.  Per-tick assertions:
#   a. 1 PENDING root, 1 WAITING mid, 1 WAITING sink1, 1 WAITING sink2 armed per tick.
#   b. mid.wait_for_run_id == root_run.run_id (current tick's root, not stale).
#   c. sink*.wait_for_run_id == mid_run.run_id (current tick's mid, not stale).
#   d. After execution, sink1 and sink2 both report received_from_run_id == mid_run.run_id.
#
# Self-healing wiring (trigger_on_status=ANY): mid and sinks all use "ANY", so a FAILED
# upstream would still drive the downstream branch. This test only drives the SUCCEEDED
# path each tick; the FAILED-upstream→ANY→PENDING flip itself is proven in
# test_chain_watcher.py::test_all_9_combinations (the ("FAILED", "ANY", "PENDING") case).


async def _get_runs_by_status_and_job(
    factory: async_sessionmaker,
    job_id: int,
    status: str,
) -> list[JobRun]:
    """Return all runs for a given job_id in the given status."""
    async with factory() as session:
        async with session.begin():
            return (
                (
                    await session.execute(
                        select(JobRun).where(
                            JobRun.job_id == job_id,
                            JobRun.status == status,
                        )
                    )
                )
                .scalars()
                .all()
            )


async def _mark_run_succeeded(
    factory: async_sessionmaker,
    run: JobRun,
    result: dict,
) -> RunEvent:
    """Mark a run SUCCEEDED and emit RunEvent. Returns committed RunEvent."""
    now = datetime.now(tz=UTC)
    async with factory() as session:
        async with session.begin():
            await session.execute(
                update(JobRun)
                .where(JobRun.run_id == run.run_id)
                .values(status="SUCCEEDED", finish_at=now, result=json.dumps(result))
            )
            event = RunEvent(
                run_id=run.run_id,
                job_id=run.job_id,
                event_type="SUCCEEDED",
                status_from="RUNNING",
                status_to="SUCCEEDED",
                occurred_at=now,
            )
            session.add(event)
            await session.flush()
            event_id = event.event_id
    async with factory() as session:
        async with session.begin():
            return (
                await session.execute(select(RunEvent).where(RunEvent.event_id == event_id))
            ).scalar_one()


@pytest.mark.integration
async def test_fan_out_digest_shape_refires_per_tick(session_factory, sqs):
    """Full fan-out digest shape: root→mid→(sink1+sink2) re-fires end-to-end per tick.

    S2 / issue #226 AC:
    - Every tick produces runs at all four levels (root, mid, sink1, sink2).
    - Fan-out: one mid run arms WAITING runs for BOTH sink1 and sink2.
    - Each run reads its own tick's direct-upstream result (not a stale one).
    - Self-healing wiring: mid and sinks use trigger_on_status=ANY (so a FAILED upstream
      would still drive the branch). This test drives only the SUCCEEDED path per tick;
      the FAILED→ANY flip is covered by test_chain_watcher.py::test_all_9_combinations.
    - No schema/API change (uses only existing Job fields).
    """
    now = datetime.now(tz=UTC)
    initial_bucket = now.replace(minute=0, second=0, microsecond=0).isoformat()

    # -----------------------------------------------------------------------
    # Setup: 4 jobs — root (cron), mid, sink1, sink2 (all trigger-driven, no cron)
    # -----------------------------------------------------------------------
    async with session_factory() as session:
        async with session.begin():
            # Root: recurring cron job (the github_digest analogue)
            job_root = Job(
                user_id="fanout-digest-test",
                description="root recurring (github_digest analogue)",
                action="capturing_chain",
                action_params={},
                job_type="recurring",
                cron_expr="@hourly",
                timezone="UTC",
            )
            session.add(job_root)
            await session.flush()

            # Root's initial run (PENDING — completed per tick)
            run_root0 = JobRun(
                time_bucket=initial_bucket,
                job_id=job_root.job_id,
                user_id=job_root.user_id,
                scheduled_at=now,
                status="PENDING",
            )
            session.add(run_root0)
            await session.flush()
            session.add(
                RunEvent(
                    run_id=run_root0.run_id,
                    job_id=job_root.job_id,
                    event_type="CREATED",
                    status_from=None,
                    status_to="PENDING",
                )
            )

            # Mid: trigger-driven off root (llm_summarize analogue); NO cron.
            job_mid = Job(
                user_id="fanout-digest-test",
                description="mid trigger-driven (llm_summarize analogue)",
                action="capturing_chain",
                action_params={},
                job_type="one_shot",
                scheduled_at=now,
                trigger_on_job_id=job_root.job_id,
                trigger_on_status="ANY",
            )
            session.add(job_mid)
            await session.flush()

            # Mid's initial WAITING run (armed against root's first run)
            run_mid0 = JobRun(
                time_bucket=initial_bucket,
                job_id=job_mid.job_id,
                user_id=job_mid.user_id,
                scheduled_at=now,
                status="WAITING",
                wait_for_run_id=run_root0.run_id,
            )
            session.add(run_mid0)
            await session.flush()
            session.add(
                RunEvent(
                    run_id=run_mid0.run_id,
                    job_id=job_mid.job_id,
                    event_type="CREATED",
                    status_from=None,
                    status_to="WAITING",
                )
            )

            # Sink1: trigger-driven off mid (slack_post analogue); NO cron.
            job_sink1 = Job(
                user_id="fanout-digest-test",
                description="sink1 (slack_post analogue)",
                action="capturing_chain",
                action_params={},
                job_type="one_shot",
                scheduled_at=now,
                trigger_on_job_id=job_mid.job_id,
                trigger_on_status="ANY",
            )
            session.add(job_sink1)
            await session.flush()

            # Sink2: trigger-driven off mid (email_send analogue); NO cron.
            job_sink2 = Job(
                user_id="fanout-digest-test",
                description="sink2 (email_send analogue)",
                action="capturing_chain",
                action_params={},
                job_type="one_shot",
                scheduled_at=now,
                trigger_on_job_id=job_mid.job_id,
                trigger_on_status="ANY",
            )
            session.add(job_sink2)
            await session.flush()

            # Sink1 + sink2 initial WAITING runs (armed against mid's initial run)
            run_sink1_0 = JobRun(
                time_bucket=initial_bucket,
                job_id=job_sink1.job_id,
                user_id=job_sink1.user_id,
                scheduled_at=now,
                status="WAITING",
                wait_for_run_id=run_mid0.run_id,
            )
            session.add(run_sink1_0)
            await session.flush()
            session.add(
                RunEvent(
                    run_id=run_sink1_0.run_id,
                    job_id=job_sink1.job_id,
                    event_type="CREATED",
                    status_from=None,
                    status_to="WAITING",
                )
            )

            run_sink2_0 = JobRun(
                time_bucket=initial_bucket,
                job_id=job_sink2.job_id,
                user_id=job_sink2.user_id,
                scheduled_at=now,
                status="WAITING",
                wait_for_run_id=run_mid0.run_id,
            )
            session.add(run_sink2_0)
            await session.flush()
            session.add(
                RunEvent(
                    run_id=run_sink2_0.run_id,
                    job_id=job_sink2.job_id,
                    event_type="CREATED",
                    status_from=None,
                    status_to="WAITING",
                )
            )

    # -----------------------------------------------------------------------
    # Drive 3 ticks
    # -----------------------------------------------------------------------
    # Track per-tick run_ids for final cross-verification
    root_run_ids: list[int] = []
    mid_run_ids: list[int] = []
    sink1_run_ids: list[int] = []
    sink2_run_ids: list[int] = []

    root_current = run_root0
    mid_current_waiting = run_mid0

    for tick in range(3):
        # --- Step 1: complete root's current run ---
        await _mark_run_succeeded(
            session_factory,
            root_current,
            result={"tick": tick, "source": "root"},
        )
        root_run_ids.append(root_current.run_id)

        # --- Step 2: chain_watcher flips mid WAITING→PENDING ---
        # (root just succeeded, mid's WAITING run wait_for_run_id == root_current.run_id)
        cw_count = await chain_poll_once(session_factory)
        assert cw_count >= 1, f"tick {tick}: chain_watcher processed 0 events (mid flip)"

        mid_pending = await _get_runs_by_status_and_job(session_factory, job_mid.job_id, "PENDING")
        assert len(mid_pending) == 1, (
            f"tick {tick}: expected exactly 1 PENDING mid run, got {len(mid_pending)}"
        )
        mid_current_pending = mid_pending[0]
        assert mid_current_pending.run_id == mid_current_waiting.run_id, (
            f"tick {tick}: expected mid run_id={mid_current_waiting.run_id} to be PENDING"
        )

        # --- Step 3: execute mid run ---
        msg = _sqs_message(mid_current_pending.run_id, job_mid.job_id)
        await process_one(session_factory, sqs, msg, registry=_REGISTRY)
        mid_run_ids.append(mid_current_pending.run_id)

        # Verify mid run SUCCEEDED and captured its upstream (root) run_id
        async with session_factory() as session:
            async with session.begin():
                mid_done = (
                    await session.execute(
                        select(JobRun).where(JobRun.run_id == mid_current_pending.run_id)
                    )
                ).scalar_one()
        assert mid_done.status == "SUCCEEDED", (
            f"tick {tick}: mid run status={mid_done.status}, expected SUCCEEDED"
        )
        mid_result = json.loads(mid_done.result)
        assert mid_result["received_from_run_id"] == root_current.run_id, (
            f"tick {tick}: mid received from_run_id={mid_result['received_from_run_id']},"
            f" expected root run_id={root_current.run_id}"
        )

        # --- Step 4: chain_watcher flips sink1 + sink2 WAITING→PENDING ---
        # (mid just succeeded; both sinks WAITING on mid)
        cw_count2 = await chain_poll_once(session_factory)
        assert cw_count2 >= 1, f"tick {tick}: chain_watcher processed 0 events (sink flip)"

        sink1_pending = await _get_runs_by_status_and_job(
            session_factory, job_sink1.job_id, "PENDING"
        )
        sink2_pending = await _get_runs_by_status_and_job(
            session_factory, job_sink2.job_id, "PENDING"
        )
        assert len(sink1_pending) == 1, (
            f"tick {tick}: expected 1 PENDING sink1 run, got {len(sink1_pending)}"
        )
        assert len(sink2_pending) == 1, (
            f"tick {tick}: expected 1 PENDING sink2 run, got {len(sink2_pending)}"
        )

        # --- Step 5: execute sink1 and sink2 ---
        for sink_run, sink_job_id, sink_run_ids in [
            (sink1_pending[0], job_sink1.job_id, sink1_run_ids),
            (sink2_pending[0], job_sink2.job_id, sink2_run_ids),
        ]:
            msg = _sqs_message(sink_run.run_id, sink_job_id)
            await process_one(session_factory, sqs, msg, registry=_REGISTRY)
            sink_run_ids.append(sink_run.run_id)

        # --- Step 6: recurring_watcher sees root's terminal event ---
        # → materialize_successor spawns root(next) + arms mid(next) + sink1(next) + sink2(next)
        rw_count = await recurring_poll_once(session_factory)
        assert rw_count >= 1, f"tick {tick}: recurring_watcher processed 0 events"

        if tick < 2:
            # Verify new root PENDING run exists
            new_root_pending = await _get_runs_by_status_and_job(
                session_factory, job_root.job_id, "PENDING"
            )
            assert len(new_root_pending) == 1, (
                f"tick {tick}: expected 1 new PENDING root run, got {len(new_root_pending)}"
            )
            root_current = new_root_pending[0]

            # KEY: verify mid has a new WAITING run pointing at the new root run
            new_mid_waiting = await _get_runs_by_status_and_job(
                session_factory, job_mid.job_id, "WAITING"
            )
            assert len(new_mid_waiting) == 1, (
                f"tick {tick}: expected 1 new WAITING mid run (from _arm cascade), "
                f"got {len(new_mid_waiting)}.  _arm must cascade through root→mid."
            )
            mid_current_waiting = new_mid_waiting[0]
            assert mid_current_waiting.wait_for_run_id == root_current.run_id, (
                f"tick {tick}: mid WAITING run points at"
                f" run_id={mid_current_waiting.wait_for_run_id},"
                f" expected new root run_id={root_current.run_id}.  Cascade must be atomic."
            )

            # KEY: verify sink1 and sink2 both have new WAITING runs pointing at the new mid run
            new_sink1_waiting = await _get_runs_by_status_and_job(
                session_factory, job_sink1.job_id, "WAITING"
            )
            new_sink2_waiting = await _get_runs_by_status_and_job(
                session_factory, job_sink2.job_id, "WAITING"
            )
            assert len(new_sink1_waiting) == 1, (
                f"tick {tick}: expected 1 WAITING sink1 run, got {len(new_sink1_waiting)}."
                "  Fan-out from mid must arm sink1."
            )
            assert len(new_sink2_waiting) == 1, (
                f"tick {tick}: expected 1 WAITING sink2 run, got {len(new_sink2_waiting)}."
                "  Fan-out from mid must arm sink2."
            )
            assert new_sink1_waiting[0].wait_for_run_id == mid_current_waiting.run_id, (
                f"tick {tick}: sink1 points at run_id={new_sink1_waiting[0].wait_for_run_id},"
                f" expected mid run_id={mid_current_waiting.run_id}"
            )
            assert new_sink2_waiting[0].wait_for_run_id == mid_current_waiting.run_id, (
                f"tick {tick}: sink2 points at run_id={new_sink2_waiting[0].wait_for_run_id},"
                f" expected mid run_id={mid_current_waiting.run_id}"
            )

    # -----------------------------------------------------------------------
    # Final cross-verification: each sink run received ITS OWN tick's mid run_id
    # -----------------------------------------------------------------------
    async with session_factory() as session:
        async with session.begin():
            all_sink1_succeeded = (
                (
                    await session.execute(
                        select(JobRun).where(
                            JobRun.job_id == job_sink1.job_id,
                            JobRun.status == "SUCCEEDED",
                        )
                    )
                )
                .scalars()
                .all()
            )
            all_sink2_succeeded = (
                (
                    await session.execute(
                        select(JobRun).where(
                            JobRun.job_id == job_sink2.job_id,
                            JobRun.status == "SUCCEEDED",
                        )
                    )
                )
                .scalars()
                .all()
            )

    assert len(all_sink1_succeeded) == 3, (
        f"expected 3 SUCCEEDED sink1 runs (one per tick), got {len(all_sink1_succeeded)}"
    )
    assert len(all_sink2_succeeded) == 3, (
        f"expected 3 SUCCEEDED sink2 runs (one per tick), got {len(all_sink2_succeeded)}"
    )

    sink1_by_id = {r.run_id: json.loads(r.result) for r in all_sink1_succeeded}
    sink2_by_id = {r.run_id: json.loads(r.result) for r in all_sink2_succeeded}

    for tick in range(3):
        expected_mid_id = mid_run_ids[tick]

        s1_result = sink1_by_id[sink1_run_ids[tick]]
        assert s1_result["received_from_run_id"] == expected_mid_id, (
            f"tick {tick}: sink1 run_id={sink1_run_ids[tick]} received "
            f"from_run_id={s1_result['received_from_run_id']},"
            f" expected mid run_id={expected_mid_id}"
        )

        s2_result = sink2_by_id[sink2_run_ids[tick]]
        assert s2_result["received_from_run_id"] == expected_mid_id, (
            f"tick {tick}: sink2 run_id={sink2_run_ids[tick]} received "
            f"from_run_id={s2_result['received_from_run_id']},"
            f" expected mid run_id={expected_mid_id}"
        )


async def _mark_run_failed(factory: async_sessionmaker, run: JobRun) -> RunEvent:
    """Mark a run FAILED and emit RunEvent(FAILED). Returns the committed RunEvent."""
    now = datetime.now(tz=UTC)
    async with factory() as session:
        async with session.begin():
            await session.execute(
                update(JobRun)
                .where(JobRun.run_id == run.run_id)
                .values(status="FAILED", finish_at=now)
            )
            event = RunEvent(
                run_id=run.run_id,
                job_id=run.job_id,
                event_type="FAILED",
                status_from="RUNNING",
                status_to="FAILED",
                occurred_at=now,
            )
            session.add(event)
            await session.flush()
            event_id = event.event_id
    async with factory() as session:
        async with session.begin():
            return (
                await session.execute(select(RunEvent).where(RunEvent.event_id == event_id))
            ).scalar_one()


@pytest.mark.integration
async def test_fan_out_self_heals_on_failed_upstream_tick(session_factory, sqs):
    """Self-healing (ADR-033): a FAILED upstream tick still drives the full downstream cascade.

    S2 / issue #226 self-healing AC. The chain is wired trigger_on_status="ANY", so a FAILED
    root must NOT stall the digest: chain_watcher flips the WAITING mid -> PENDING (ANY matches
    FAILED — not left WAITING, not dropped), mid executes reading the *failed* root's run_id
    (its error/fallback branch), and BOTH sinks then flip + run -> the sink still notifies.

    Complements test_fan_out_digest_shape_refires_per_tick, which drives only the SUCCEEDED
    path; this isolates the FAILED-upstream -> ANY -> cascade-completes behaviour end to end.
    """
    now = datetime.now(tz=UTC)
    bucket = now.replace(minute=0, second=0, microsecond=0).isoformat()

    async with session_factory() as session:
        async with session.begin():
            job_root = Job(
                user_id="self-heal-test",
                description="root recurring",
                action="capturing_chain",
                action_params={},
                job_type="recurring",
                cron_expr="@hourly",
                timezone="UTC",
            )
            session.add(job_root)
            await session.flush()

            run_root0 = JobRun(
                time_bucket=bucket,
                job_id=job_root.job_id,
                user_id=job_root.user_id,
                scheduled_at=now,
                status="PENDING",
            )
            session.add(run_root0)
            await session.flush()
            session.add(
                RunEvent(
                    run_id=run_root0.run_id,
                    job_id=job_root.job_id,
                    event_type="CREATED",
                    status_from=None,
                    status_to="PENDING",
                )
            )

            job_mid = Job(
                user_id="self-heal-test",
                description="mid trigger-driven (ANY)",
                action="capturing_chain",
                action_params={},
                job_type="one_shot",
                scheduled_at=now,
                trigger_on_job_id=job_root.job_id,
                trigger_on_status="ANY",
            )
            session.add(job_mid)
            await session.flush()

            run_mid0 = JobRun(
                time_bucket=bucket,
                job_id=job_mid.job_id,
                user_id=job_mid.user_id,
                scheduled_at=now,
                status="WAITING",
                wait_for_run_id=run_root0.run_id,
            )
            session.add(run_mid0)
            await session.flush()
            session.add(
                RunEvent(
                    run_id=run_mid0.run_id,
                    job_id=job_mid.job_id,
                    event_type="CREATED",
                    status_from=None,
                    status_to="WAITING",
                )
            )

            job_sink1 = Job(
                user_id="self-heal-test",
                description="sink1 (ANY)",
                action="capturing_chain",
                action_params={},
                job_type="one_shot",
                scheduled_at=now,
                trigger_on_job_id=job_mid.job_id,
                trigger_on_status="ANY",
            )
            session.add(job_sink1)
            await session.flush()

            job_sink2 = Job(
                user_id="self-heal-test",
                description="sink2 (ANY)",
                action="capturing_chain",
                action_params={},
                job_type="one_shot",
                scheduled_at=now,
                trigger_on_job_id=job_mid.job_id,
                trigger_on_status="ANY",
            )
            session.add(job_sink2)
            await session.flush()

            run_sink1_0 = JobRun(
                time_bucket=bucket,
                job_id=job_sink1.job_id,
                user_id=job_sink1.user_id,
                scheduled_at=now,
                status="WAITING",
                wait_for_run_id=run_mid0.run_id,
            )
            session.add(run_sink1_0)
            run_sink2_0 = JobRun(
                time_bucket=bucket,
                job_id=job_sink2.job_id,
                user_id=job_sink2.user_id,
                scheduled_at=now,
                status="WAITING",
                wait_for_run_id=run_mid0.run_id,
            )
            session.add(run_sink2_0)
            await session.flush()
            session.add(
                RunEvent(
                    run_id=run_sink1_0.run_id,
                    job_id=job_sink1.job_id,
                    event_type="CREATED",
                    status_from=None,
                    status_to="WAITING",
                )
            )
            session.add(
                RunEvent(
                    run_id=run_sink2_0.run_id,
                    job_id=job_sink2.job_id,
                    event_type="CREATED",
                    status_from=None,
                    status_to="WAITING",
                )
            )

    # --- Root FAILS this tick ---
    await _mark_run_failed(session_factory, run_root0)

    # --- Self-heal: chain_watcher flips mid WAITING -> PENDING despite the FAILED root ---
    await chain_poll_once(session_factory)
    mid_pending = await _get_runs_by_status_and_job(session_factory, job_mid.job_id, "PENDING")
    assert len(mid_pending) == 1, (
        "self-heal: a FAILED root (trigger_on_status=ANY) must still flip mid "
        f"WAITING->PENDING, got {len(mid_pending)} PENDING mid runs"
    )
    assert mid_pending[0].wait_for_run_id == run_root0.run_id

    mid_waiting = await _get_runs_by_status_and_job(session_factory, job_mid.job_id, "WAITING")
    assert len(mid_waiting) == 0, "mid must not be left stuck WAITING after a FAILED upstream"
    mid_cancelled = await _get_runs_by_status_and_job(session_factory, job_mid.job_id, "CANCELLED")
    assert len(mid_cancelled) == 0, "single cascade has no overlap — mid must not be dropped"

    # --- mid executes, reading the FAILED root's run_id (its error/fallback branch) ---
    await process_one(
        session_factory,
        sqs,
        _sqs_message(mid_pending[0].run_id, job_mid.job_id),
        registry=_REGISTRY,
    )
    async with session_factory() as session:
        async with session.begin():
            mid_done = (
                await session.execute(select(JobRun).where(JobRun.run_id == mid_pending[0].run_id))
            ).scalar_one()
    assert mid_done.status == "SUCCEEDED", f"mid status={mid_done.status}, expected SUCCEEDED"
    assert json.loads(mid_done.result)["received_from_run_id"] == run_root0.run_id, (
        "mid must read the FAILED root's run_id (self-heal reads upstream for its fallback)"
    )

    # --- both sinks flip + run -> the sink still notifies despite the upstream failure ---
    await chain_poll_once(session_factory)
    sink1_pending = await _get_runs_by_status_and_job(session_factory, job_sink1.job_id, "PENDING")
    sink2_pending = await _get_runs_by_status_and_job(session_factory, job_sink2.job_id, "PENDING")
    assert len(sink1_pending) == 1 and len(sink2_pending) == 1, (
        "self-heal: both sinks must be driven after a failed upstream tick "
        f"(sink1={len(sink1_pending)}, sink2={len(sink2_pending)})"
    )

    for sink_run, sink_job_id in [
        (sink1_pending[0], job_sink1.job_id),
        (sink2_pending[0], job_sink2.job_id),
    ]:
        await process_one(
            session_factory,
            sqs,
            _sqs_message(sink_run.run_id, sink_job_id),
            registry=_REGISTRY,
        )

    async with session_factory() as session:
        async with session.begin():
            sink1_done = (
                await session.execute(
                    select(JobRun).where(JobRun.run_id == sink1_pending[0].run_id)
                )
            ).scalar_one()
            sink2_done = (
                await session.execute(
                    select(JobRun).where(JobRun.run_id == sink2_pending[0].run_id)
                )
            ).scalar_one()
    assert sink1_done.status == "SUCCEEDED", "sink1 must notify even when the upstream tick FAILED"
    assert sink2_done.status == "SUCCEEDED", "sink2 must notify even when the upstream tick FAILED"
