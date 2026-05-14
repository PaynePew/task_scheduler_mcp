"""E2E test: 6-step MCP Inspector verification flow (W1 acceptance gate).

Mirrors the manual steps from PROMPT.md § Verification step 2:
  1. Connect → 5 tools visible
  2. task.create@v1 (immediate echo) → job_id + status "scheduled"
  3. Watcher + Worker run → task.status@v1 → status "completed"
  4. task.create@v1 (2099-12-31, one-shot) → job_id + status "scheduled"
  5. task.cancel@v1 → status "cancelled"
  6. task.list@v1 → both jobs visible

The test is the "CI equivalent" of docker compose --profile full up: it runs
all six process roles in-process (ASGITransport for the MCP HTTP server,
claim_and_publish for the Watcher, process_one for the Worker) against the
real Postgres + ElasticMQ instances brought up by .harness/before-tests.sh.

Run with:
    uv run pytest -m integration tests/integration/test_e2e_inspector_flow.py
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
from app.queue.sqs import SQSClient
from app.workers.executor import process_one
from app.workers.watcher import claim_and_publish

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_REQUEST_ID = 0


def _next_id() -> int:
    global _REQUEST_ID
    _REQUEST_ID += 1
    return _REQUEST_ID


MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
}

_E2E_USER = "e2e-inspector-user"


async def _mcp_call(
    client: httpx.AsyncClient,
    tool: str,
    arguments: dict,
) -> dict:
    """Send tools/call and return the parsed result dict."""
    body = {
        "jsonrpc": "2.0",
        "id": _next_id(),
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }
    headers = {**MCP_HEADERS, "X-User-Id": _E2E_USER}
    resp = await client.post("/mcp", json=body, headers=headers)
    resp.raise_for_status()
    data = resp.json()
    return json.loads(data["result"]["content"][0]["text"])


async def _list_tools(client: httpx.AsyncClient) -> list[dict]:
    """Send tools/list and return the tools array."""
    body = {
        "jsonrpc": "2.0",
        "id": _next_id(),
        "method": "tools/list",
        "params": {},
    }
    headers = {**MCP_HEADERS, "X-User-Id": _E2E_USER}
    resp = await client.post("/mcp", json=body, headers=headers)
    resp.raise_for_status()
    data = resp.json()
    return data["result"]["tools"]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def session_factory() -> async_sessionmaker[AsyncSession]:
    """Fresh engine; cleans all job data on teardown."""
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
def sqs(session_factory) -> SQSClient:
    """SQSClient pointed at ElasticMQ; drains leftover messages before test."""
    client = SQSClient()
    while True:
        msgs = client.receive_messages(max_messages=10, wait_seconds=0)
        if not msgs:
            break
        for msg in msgs:
            client.delete_message(msg["ReceiptHandle"])
    return client


@pytest.fixture
def mcp_client(session_factory) -> httpx.AsyncClient:
    """In-process httpx client pointed at the MCP HTTP ASGI app (json_response mode)."""
    app = build_app(json_response=True, session_factory=session_factory)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_e2e_inspector_flow(mcp_client, session_factory, sqs):
    """Full 6-step W1 acceptance gate: mirrors the PROMPT.md Inspector flow."""
    async with mcp_client as client:
        # ------------------------------------------------------------------
        # Step 1: Connect → exactly 5 tools visible
        # ------------------------------------------------------------------
        tools = await _list_tools(client)
        tool_names = {t["name"] for t in tools}
        assert tool_names == {
            "task.create@v1",
            "task.list@v1",
            "task.status@v1",
            "task.cancel@v1",
            "task.list_actions@v1",
        }, f"expected 5 tools, got: {tool_names}"

        # ------------------------------------------------------------------
        # Step 2: task.create@v1 (immediate echo) → job_id + status "scheduled"
        # ------------------------------------------------------------------
        create_imm = await _mcp_call(
            client,
            "task.create@v1",
            {
                "action": "echo",
                "action_params": {"message": "e2e inspector test"},
                "schedule_type": "immediate",
            },
        )
        assert create_imm["ok"] is True, f"step 2 create failed: {create_imm}"
        job_id_1: int = create_imm["data"]["job_id"]
        assert create_imm["data"]["status"] == "scheduled", (
            f"step 2: expected 'scheduled', got {create_imm['data']['status']!r}"
        )

        # ------------------------------------------------------------------
        # Step 3: Watcher claims + Worker executes → status "completed"
        # (In-process CI equivalent of waiting ~10 s with full Compose stack)
        # ------------------------------------------------------------------
        claimed = await claim_and_publish(session_factory, sqs)
        assert claimed >= 1, "watcher should have claimed at least one run"

        msgs = sqs.receive_messages(max_messages=1, wait_seconds=2)
        assert len(msgs) == 1, "SQS should have one message after watcher tick"

        await process_one(session_factory, sqs, msgs[0])

        status_1 = await _mcp_call(client, "task.status@v1", {"job_id": job_id_1})
        assert status_1["ok"] is True, f"step 3 status failed: {status_1}"
        assert status_1["data"]["status"] == "completed", (
            f"step 3: expected 'completed', got {status_1['data']['status']!r}"
        )

        # ------------------------------------------------------------------
        # Step 4: task.create@v1 (far-future one-shot) → job_id + "scheduled"
        # ------------------------------------------------------------------
        create_future = await _mcp_call(
            client,
            "task.create@v1",
            {
                "action": "echo",
                "action_params": {"message": "far future"},
                "schedule_type": "one-shot",
                "scheduled_at": "2099-12-31T00:00:00+00:00",
                "timezone": "UTC",
            },
        )
        assert create_future["ok"] is True, f"step 4 create failed: {create_future}"
        job_id_2: int = create_future["data"]["job_id"]
        assert create_future["data"]["status"] == "scheduled", (
            f"step 4: expected 'scheduled', got {create_future['data']['status']!r}"
        )

        # ------------------------------------------------------------------
        # Step 5: task.cancel@v1 → status "cancelled"
        # ------------------------------------------------------------------
        cancel = await _mcp_call(client, "task.cancel@v1", {"job_id": job_id_2})
        assert cancel["ok"] is True, f"step 5 cancel failed: {cancel}"
        assert cancel["data"]["status"] == "cancelled", (
            f"step 5: expected 'cancelled', got {cancel['data']['status']!r}"
        )

        # ------------------------------------------------------------------
        # Step 6: task.list@v1 → both jobs visible
        # ------------------------------------------------------------------
        list_result = await _mcp_call(client, "task.list@v1", {})
        assert list_result["ok"] is True, f"step 6 list failed: {list_result}"
        visible_ids = {j["job_id"] for j in list_result["data"]["jobs"]}
        assert job_id_1 in visible_ids, f"step 6: job_id_1={job_id_1} not in {visible_ids}"
        assert job_id_2 in visible_ids, f"step 6: job_id_2={job_id_2} not in {visible_ids}"
