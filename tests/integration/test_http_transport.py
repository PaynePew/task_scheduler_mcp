"""Integration test: Streamable HTTP transport + X-User-Id cross-tenant isolation.

Run with:
    uv run pytest -m integration tests/integration/test_http_transport.py

The app is started in-process via httpx.ASGITransport so no real TCP port is needed.
json_response=True is used so responses are plain JSON (not SSE), keeping assertions simple
while still exercising the same DB + user-isolation code path.
"""

from __future__ import annotations

import json

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.engine import create_async_engine
from app.entrypoints.mcp_http import build_app

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
}

_REQUEST_ID = 0


def _next_id() -> int:
    global _REQUEST_ID
    _REQUEST_ID += 1
    return _REQUEST_ID


def _call_tool(name: str, arguments: dict) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": _next_id(),
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }


async def _mcp_call(
    client: httpx.AsyncClient,
    tool: str,
    arguments: dict,
    *,
    x_user_id: str | None = None,
) -> dict:
    """Send a single stateless tools/call and return the parsed MCP result."""
    headers = dict(MCP_HEADERS)
    if x_user_id is not None:
        headers["X-User-Id"] = x_user_id

    resp = await client.post("/mcp", json=_call_tool(tool, arguments), headers=headers)
    resp.raise_for_status()
    body = resp.json()
    # tools/call result is in body["result"]["content"][0]["text"] as JSON string
    content_text = body["result"]["content"][0]["text"]
    return json.loads(content_text)


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


@pytest.fixture
def mcp_client(session_factory) -> httpx.AsyncClient:
    """In-process httpx client pointed at the MCP HTTP ASGI app (json_response mode).

    Passes the per-test session_factory so the HTTP handler avoids asyncpg
    cross-event-loop errors that arise when the module-level engine pool is
    reused across pytest-asyncio function-scoped event loops.
    """
    app = build_app(json_response=True, session_factory=session_factory)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_create_job_via_http(mcp_client, session_factory):
    """HTTP client with X-User-Id: alice can create a job."""
    async with mcp_client as client:
        result = await _mcp_call(
            client,
            "task.create.v1",
            {"action": "echo", "action_params": {"message": "hi"}},
            x_user_id="alice",
        )
    assert result["ok"] is True
    assert "job_id" in result["data"]


@pytest.mark.integration
async def test_cross_tenant_isolation_not_found(mcp_client, session_factory):
    """alice creates a job; bob's task.status.v1 call returns NOT_FOUND (hard isolation).

    This test is the primary AC: same HTTP server, two different X-User-Id values,
    bob cannot read alice's job_id via task.status.v1.
    """
    async with mcp_client as client:
        # Step 1: alice creates a job
        create_result = await _mcp_call(
            client,
            "task.create.v1",
            {"action": "echo", "action_params": {"message": "alice's job"}},
            x_user_id="alice",
        )
        assert create_result["ok"] is True, f"create failed: {create_result}"
        job_id = create_result["data"]["job_id"]

        # Step 2: bob tries to read alice's job via task.status.v1 → NOT_FOUND
        status_result = await _mcp_call(
            client,
            "task.status.v1",
            {"job_id": job_id},
            x_user_id="bob",
        )
        assert status_result["ok"] is False
        assert status_result["error"]["code"] == "NOT_FOUND"

        # Step 3: alice can read her own job → ok
        own_status = await _mcp_call(
            client,
            "task.status.v1",
            {"job_id": job_id},
            x_user_id="alice",
        )
        assert own_status["ok"] is True
        assert own_status["data"]["job_id"] == job_id


@pytest.mark.integration
async def test_no_header_falls_back_to_default_user(mcp_client, session_factory):
    """No X-User-Id header → resolves to 'default-user'; isolation still holds.

    default-user cannot read alice's job.
    """
    async with mcp_client as client:
        # alice creates a job
        create_result = await _mcp_call(
            client,
            "task.create.v1",
            {"action": "echo", "action_params": {"message": "alice only"}},
            x_user_id="alice",
        )
        assert create_result["ok"] is True
        job_id = create_result["data"]["job_id"]

        # default-user (no header) cannot read alice's job
        status_result = await _mcp_call(
            client,
            "task.status.v1",
            {"job_id": job_id},
            x_user_id=None,  # no header → "default-user"
        )
        assert status_result["ok"] is False
        assert status_result["error"]["code"] == "NOT_FOUND"
