"""Integration tests for task.list@v1 — requires running Postgres.

Run with:
    uv run pytest -m integration tests/integration/test_list.py
"""

from __future__ import annotations

from datetime import datetime

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.engine import create_async_engine
from app.db.models import Job, JobRun
from app.domain.jobs import create_job
from app.mcp.handlers.list import handle_task_list

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def session_factory() -> async_sessionmaker[AsyncSession]:
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


async def _make_job(
    factory: async_sessionmaker[AsyncSession],
    *,
    user_id: str = "list-test-user",
    idempotency_key: str | None = None,
) -> Job:
    async with factory() as session:
        return await create_job(
            session,
            user_id=user_id,
            action="echo",
            action_params={"message": "hi"},
            schedule_type="immediate",
            idempotency_key=idempotency_key,
        )


async def _force_run_status(
    factory: async_sessionmaker[AsyncSession],
    job_id: int,
    status: str,
) -> None:
    """Directly set all job_runs for a job to a given status (for test setup)."""
    async with factory() as session:
        async with session.begin():
            await session.execute(
                update(JobRun).where(JobRun.job_id == job_id).values(status=status)
            )


# ---------------------------------------------------------------------------
# Basic list tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_list_returns_own_jobs(session_factory) -> None:
    """User gets back their own jobs."""
    job = await _make_job(session_factory, user_id="list-test-user")

    result = await handle_task_list({}, user_id="list-test-user", session_factory=session_factory)

    assert result["ok"] is True
    job_ids = [j["job_id"] for j in result["data"]["jobs"]]
    assert job.job_id in job_ids


@pytest.mark.integration
async def test_list_excludes_other_users_jobs(session_factory) -> None:
    """Cross-user jobs never appear in the caller's list."""
    other_job = await _make_job(session_factory, user_id="other-user")

    result = await handle_task_list({}, user_id="list-test-user", session_factory=session_factory)

    assert result["ok"] is True
    job_ids = [j["job_id"] for j in result["data"]["jobs"]]
    assert other_job.job_id not in job_ids


@pytest.mark.integration
async def test_list_sorted_newest_first(session_factory) -> None:
    """Results are ordered newest-first (idx_jobs_user_created)."""
    job1 = await _make_job(session_factory, user_id="sort-user")
    job2 = await _make_job(session_factory, user_id="sort-user")
    job3 = await _make_job(session_factory, user_id="sort-user")

    result = await handle_task_list({}, user_id="sort-user", session_factory=session_factory)

    assert result["ok"] is True
    ids = [j["job_id"] for j in result["data"]["jobs"]]
    # Newest job (highest job_id / latest created_at) should come first
    assert ids.index(job3.job_id) < ids.index(job2.job_id) < ids.index(job1.job_id)


# ---------------------------------------------------------------------------
# Pagination tests (30+ jobs)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_pagination_stable_non_overlapping(session_factory) -> None:
    """30 jobs split over 3 pages of 10 — non-overlapping and covers all."""
    for i in range(30):
        await _make_job(session_factory, user_id="page-user", idempotency_key=f"page-key-{i}")

    # Collect all job IDs across 3 pages
    seen: set[int] = set()
    for page in range(1, 4):
        result = await handle_task_list(
            {"page": page, "pageSize": 10},
            user_id="page-user",
            session_factory=session_factory,
        )
        assert result["ok"] is True
        assert result["data"]["total"] == 30
        assert result["data"]["page"] == page
        assert result["data"]["pageSize"] == 10
        page_ids = {j["job_id"] for j in result["data"]["jobs"]}
        assert len(page_ids) == 10, f"page {page} returned {len(page_ids)} items"
        # Non-overlapping
        overlap = seen & page_ids
        assert not overlap, f"page {page} overlaps with earlier pages: {overlap}"
        seen |= page_ids

    assert len(seen) == 30


@pytest.mark.integration
async def test_total_reflects_filtered_count(session_factory) -> None:
    """total in response matches the count of jobs satisfying the filters."""
    for i in range(5):
        await _make_job(session_factory, user_id="total-user", idempotency_key=f"total-key-{i}")

    result = await handle_task_list(
        {"pageSize": 2, "page": 1}, user_id="total-user", session_factory=session_factory
    )
    assert result["ok"] is True
    assert result["data"]["total"] == 5
    assert len(result["data"]["jobs"]) == 2


# ---------------------------------------------------------------------------
# Status filter tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_status_filter_scheduled(session_factory) -> None:
    """status=scheduled returns only jobs whose latest run is PENDING/QUEUED/WAITING."""
    pending_job = await _make_job(session_factory, user_id="filter-user")
    succeeded_job = await _make_job(session_factory, user_id="filter-user")
    await _force_run_status(session_factory, succeeded_job.job_id, "SUCCEEDED")

    result = await handle_task_list(
        {"status": "scheduled"}, user_id="filter-user", session_factory=session_factory
    )
    assert result["ok"] is True
    ids = [j["job_id"] for j in result["data"]["jobs"]]
    assert pending_job.job_id in ids
    assert succeeded_job.job_id not in ids


@pytest.mark.integration
async def test_status_filter_completed(session_factory) -> None:
    """status=completed returns only SUCCEEDED jobs."""
    pending_job = await _make_job(session_factory, user_id="filter-user2")
    completed_job = await _make_job(session_factory, user_id="filter-user2")
    await _force_run_status(session_factory, completed_job.job_id, "SUCCEEDED")

    result = await handle_task_list(
        {"status": "completed"}, user_id="filter-user2", session_factory=session_factory
    )
    assert result["ok"] is True
    ids = [j["job_id"] for j in result["data"]["jobs"]]
    assert completed_job.job_id in ids
    assert pending_job.job_id not in ids
    # External status in response
    for job_data in result["data"]["jobs"]:
        assert job_data["status"] == "completed"


@pytest.mark.integration
async def test_status_filter_failed(session_factory) -> None:
    """status=failed returns only FAILED jobs."""
    ok_job = await _make_job(session_factory, user_id="filter-user3")
    failed_job = await _make_job(session_factory, user_id="filter-user3")
    await _force_run_status(session_factory, failed_job.job_id, "FAILED")

    result = await handle_task_list(
        {"status": "failed"}, user_id="filter-user3", session_factory=session_factory
    )
    assert result["ok"] is True
    ids = [j["job_id"] for j in result["data"]["jobs"]]
    assert failed_job.job_id in ids
    assert ok_job.job_id not in ids


@pytest.mark.integration
async def test_status_filter_cancelled(session_factory) -> None:
    """status=cancelled returns only CANCELLED jobs."""
    active_job = await _make_job(session_factory, user_id="filter-user4")
    cancelled_job = await _make_job(session_factory, user_id="filter-user4")
    await _force_run_status(session_factory, cancelled_job.job_id, "CANCELLED")

    result = await handle_task_list(
        {"status": "cancelled"}, user_id="filter-user4", session_factory=session_factory
    )
    assert result["ok"] is True
    ids = [j["job_id"] for j in result["data"]["jobs"]]
    assert cancelled_job.job_id in ids
    assert active_job.job_id not in ids


# ---------------------------------------------------------------------------
# created_at range filter tests
# ---------------------------------------------------------------------------


async def _db_now(factory: async_sessionmaker[AsyncSession]) -> datetime:
    """Read `now()` from Postgres itself.

    Why: `Job.created_at` is set by the DB via `server_default=func.now()`, so the
    cutoff must come from the same clock — host vs container clock skew (common on
    Docker Desktop / WSL2) is otherwise large enough to invert the comparison.
    """
    async with factory() as session:
        return (await session.execute(select(func.now()))).scalar_one()


@pytest.mark.integration
async def test_created_at_from_excludes_older_jobs(session_factory) -> None:
    """created_at_from excludes jobs created before the threshold."""
    job_old = await _make_job(session_factory, user_id="range-user")

    cutoff = await _db_now(session_factory)

    job_new = await _make_job(session_factory, user_id="range-user")

    result = await handle_task_list(
        {"created_at_from": cutoff.isoformat()},
        user_id="range-user",
        session_factory=session_factory,
    )
    assert result["ok"] is True
    ids = [j["job_id"] for j in result["data"]["jobs"]]
    assert job_new.job_id in ids
    assert job_old.job_id not in ids


@pytest.mark.integration
async def test_created_at_to_excludes_newer_jobs(session_factory) -> None:
    """created_at_to excludes jobs created after the threshold."""
    job_old = await _make_job(session_factory, user_id="range-user2")

    cutoff = await _db_now(session_factory)

    job_new = await _make_job(session_factory, user_id="range-user2")

    result = await handle_task_list(
        {"created_at_to": cutoff.isoformat()},
        user_id="range-user2",
        session_factory=session_factory,
    )
    assert result["ok"] is True
    ids = [j["job_id"] for j in result["data"]["jobs"]]
    assert job_old.job_id in ids
    assert job_new.job_id not in ids


# ---------------------------------------------------------------------------
# Cross-user isolation with 30+ jobs
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_cross_user_isolation_with_many_jobs(session_factory) -> None:
    """30+ jobs across two users — each user only sees their own jobs."""
    for i in range(20):
        await _make_job(session_factory, user_id="alice", idempotency_key=f"alice-key-{i}")
    for i in range(15):
        await _make_job(session_factory, user_id="bob", idempotency_key=f"bob-key-{i}")

    alice_result = await handle_task_list(
        {"pageSize": 100}, user_id="alice", session_factory=session_factory
    )
    bob_result = await handle_task_list(
        {"pageSize": 100}, user_id="bob", session_factory=session_factory
    )

    assert alice_result["ok"] is True
    assert bob_result["ok"] is True
    assert alice_result["data"]["total"] == 20
    assert bob_result["data"]["total"] == 15

    alice_ids = {j["job_id"] for j in alice_result["data"]["jobs"]}
    bob_ids = {j["job_id"] for j in bob_result["data"]["jobs"]}
    assert not alice_ids & bob_ids, "cross-user job IDs leaked"
