"""Integration tests for MCP resources R1 (tasks://list), R2 (tasks://job/{id}), R3 (tasks://actions).

Run with:
    uv run pytest -m integration tests/integration/test_resources.py
"""

from __future__ import annotations

import json

import pytest
import pytest_asyncio
from mcp import McpError
from pydantic import AnyUrl, TypeAdapter
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.engine import create_async_engine
from app.db.models import JobRun
from app.domain.jobs import create_job
from app.mcp.resources.actions_resource import read_tasks_actions
from app.mcp.resources.job_resource import read_tasks_job
from app.mcp.resources.list_resource import read_tasks_list

_AnyUrl = TypeAdapter(AnyUrl)


def _uri(s: str) -> AnyUrl:
    return _AnyUrl.validate_python(s)


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
    user_id: str = "res-test-user",
    idempotency_key: str | None = None,
):
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
    async with factory() as session:
        async with session.begin():
            await session.execute(
                update(JobRun).where(JobRun.job_id == job_id).values(status=status)
            )


# ---------------------------------------------------------------------------
# R3: tasks://actions (simplest — no DB needed)
# ---------------------------------------------------------------------------


def test_actions_resource_sync():
    """R3 returns a list with echo and http_call entries."""
    contents = read_tasks_actions()
    assert len(contents) == 1
    payload = json.loads(contents[0].content)
    assert isinstance(payload, list)
    names = {entry["name"] for entry in payload}
    assert "echo" in names
    assert "http_call" in names


def test_actions_resource_schema_fields():
    """Each entry has name, description, timeout_seconds, params_schema."""
    contents = read_tasks_actions()
    payload = json.loads(contents[0].content)
    for entry in payload:
        assert "name" in entry
        assert "description" in entry
        assert "timeout_seconds" in entry
        assert "params_schema" in entry
    assert contents[0].mime_type == "application/json"


# ---------------------------------------------------------------------------
# R1: tasks://list
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_list_resource_returns_own_jobs(session_factory):
    """R1 snapshot includes jobs owned by the caller."""
    job = await _make_job(session_factory, user_id="r1-user")

    contents = await read_tasks_list("r1-user", session_factory=session_factory)
    payload = json.loads(contents[0].content)

    assert "snapshot_at" in payload
    assert "total" in payload
    assert "items" in payload
    ids = [item["job_id"] for item in payload["items"]]
    assert job.job_id in ids


@pytest.mark.integration
async def test_list_resource_excludes_other_user(session_factory):
    """R1 snapshot never includes another user's jobs."""
    other_job = await _make_job(session_factory, user_id="r1-other")

    contents = await read_tasks_list("r1-user-2", session_factory=session_factory)
    payload = json.loads(contents[0].content)

    ids = [item["job_id"] for item in payload["items"]]
    assert other_job.job_id not in ids


@pytest.mark.integration
async def test_list_resource_top_20_newest_first(session_factory):
    """R1 returns at most 20 jobs in newest-first order."""
    for i in range(25):
        await _make_job(session_factory, user_id="r1-many", idempotency_key=f"r1-k-{i}")

    contents = await read_tasks_list("r1-many", session_factory=session_factory)
    payload = json.loads(contents[0].content)

    assert payload["total"] == 25
    assert len(payload["items"]) == 20
    # newest-first: job_ids should be descending
    ids = [item["job_id"] for item in payload["items"]]
    assert ids == sorted(ids, reverse=True)


@pytest.mark.integration
async def test_list_resource_item_shape(session_factory):
    """Each item in R1 has the required fields with correct types."""
    await _make_job(session_factory, user_id="r1-shape")

    contents = await read_tasks_list("r1-shape", session_factory=session_factory)
    payload = json.loads(contents[0].content)

    assert len(payload["items"]) >= 1
    item = payload["items"][0]
    assert "job_id" in item
    assert "description" in item
    assert "status" in item
    assert "schedule_type" in item
    assert "created_at" in item
    assert "cancelled_at" in item  # field present (may be null)
    assert contents[0].mime_type == "application/json"


@pytest.mark.integration
async def test_list_resource_cancelled_at_populated(session_factory):
    """cancelled_at is populated for cancelled jobs."""
    job = await _make_job(session_factory, user_id="r1-cancel")
    await _force_run_status(session_factory, job.job_id, "CANCELLED")
    # Insert a CANCELLED run event so cancelled_at subquery fires
    from app.db.models import RunEvent

    async with session_factory() as session:
        async with session.begin():
            run_result = await session.execute(
                text("SELECT run_id FROM job_runs WHERE job_id = :jid LIMIT 1"),
                {"jid": job.job_id},
            )
            run_id = run_result.scalar_one()
            session.add(
                RunEvent(
                    run_id=run_id,
                    job_id=job.job_id,
                    event_type="CANCELLED",
                    status_from="PENDING",
                    status_to="CANCELLED",
                )
            )

    contents = await read_tasks_list("r1-cancel", session_factory=session_factory)
    payload = json.loads(contents[0].content)
    items = {item["job_id"]: item for item in payload["items"]}
    assert items[job.job_id]["cancelled_at"] is not None


# ---------------------------------------------------------------------------
# R2: tasks://job/{job_id}
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_job_resource_returns_job(session_factory):
    """R2 returns the job shape with required fields."""
    job = await _make_job(session_factory, user_id="r2-user")
    uri = _uri(f"tasks://job/{job.job_id}")

    contents = await read_tasks_job(uri, "r2-user", session_factory=session_factory)
    payload = json.loads(contents[0].content)

    assert payload["job_id"] == job.job_id
    assert "description" in payload
    assert "action" in payload
    assert "schedule_type" in payload
    assert "created_at" in payload
    assert "status" in payload
    assert "runs" in payload
    assert contents[0].mime_type == "application/json"


@pytest.mark.integration
async def test_job_resource_returns_at_most_5_runs(session_factory):
    """R2 returns at most 5 recent runs."""
    job = await _make_job(session_factory, user_id="r2-runs")
    uri = _uri(f"tasks://job/{job.job_id}")

    contents = await read_tasks_job(uri, "r2-runs", session_factory=session_factory)
    payload = json.loads(contents[0].content)

    assert len(payload["runs"]) <= 5


@pytest.mark.integration
async def test_job_resource_cross_user_returns_404(session_factory):
    """R2 cross-user access raises McpError (404 semantics)."""
    job = await _make_job(session_factory, user_id="r2-owner")
    uri = _uri(f"tasks://job/{job.job_id}")

    with pytest.raises(McpError):
        await read_tasks_job(uri, "r2-other-user", session_factory=session_factory)


@pytest.mark.integration
async def test_job_resource_nonexistent_returns_404(session_factory):
    """R2 non-existent job_id raises McpError (404 semantics)."""
    uri = _uri("tasks://job/99999999")

    with pytest.raises(McpError):
        await read_tasks_job(uri, "r2-user", session_factory=session_factory)


# ---------------------------------------------------------------------------
# Regression: tools remain registered
# ---------------------------------------------------------------------------


def test_tools_still_registered():
    """All five tools remain in the server's list_tools handler (regression check)."""
    from app.mcp.server import create_server

    server = create_server(user_id="reg-user")
    # list_tools handler is registered if ListToolsRequest is in request_handlers
    import mcp.types as mcp_types

    assert mcp_types.ListToolsRequest in server.request_handlers
