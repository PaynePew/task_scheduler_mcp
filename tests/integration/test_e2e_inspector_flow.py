"""E2E test: MCP Inspector verification flow (W1 + W2 acceptance gate).

W1 flow (6 steps):
  1. Connect → 5 tools visible
  2. task.create.v1 (immediate echo) → job_id + status "scheduled"
  3. Watcher + Worker run → task.status.v1 → status "completed"
  4. task.create.v1 (2099-12-31, one-shot) → job_id + status "scheduled"
  5. task.cancel.v1 → status "cancelled"
  6. task.list.v1 → both jobs visible

W2 flow (steps 7-11, test_w2_bonuses):
  7. Recurring — create with cron, run + tick; assert next run spawned
  8. Cancel recurring — cancel job, tick again; assert no further spawn
  9. Chaining — A→B; complete A + chain tick; assert B completes
  10. Resources — resources/list 3 entries; tasks://list filters by user
  11. Prompts — prompts/list 2 entries; prompts/get substitutes args

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
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.engine import create_async_engine
from app.db.models import JobRun, RunEvent
from app.entrypoints.mcp_http import build_app
from app.queue.sqs import SQSClient
from app.workers.chain_watcher import poll_once as chain_watcher_tick
from app.workers.executor import process_one
from app.workers.recurring_watcher import poll_once as recurring_watcher_tick
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


async def _list_resources(client: httpx.AsyncClient) -> list[dict]:
    """Send resources/list and return the resources array."""
    body = {
        "jsonrpc": "2.0",
        "id": _next_id(),
        "method": "resources/list",
        "params": {},
    }
    headers = {**MCP_HEADERS, "X-User-Id": _E2E_USER}
    resp = await client.post("/mcp", json=body, headers=headers)
    resp.raise_for_status()
    data = resp.json()
    return data["result"]["resources"]


async def _list_resource_templates(client: httpx.AsyncClient) -> list[dict]:
    """Send resources/templates/list and return the resourceTemplates array."""
    body = {
        "jsonrpc": "2.0",
        "id": _next_id(),
        "method": "resources/templates/list",
        "params": {},
    }
    headers = {**MCP_HEADERS, "X-User-Id": _E2E_USER}
    resp = await client.post("/mcp", json=body, headers=headers)
    resp.raise_for_status()
    data = resp.json()
    return data["result"]["resourceTemplates"]


async def _read_resource(client: httpx.AsyncClient, uri: str) -> dict:
    """Send resources/read and return parsed JSON from the first content item."""
    body = {
        "jsonrpc": "2.0",
        "id": _next_id(),
        "method": "resources/read",
        "params": {"uri": uri},
    }
    headers = {**MCP_HEADERS, "X-User-Id": _E2E_USER}
    resp = await client.post("/mcp", json=body, headers=headers)
    resp.raise_for_status()
    data = resp.json()
    return json.loads(data["result"]["contents"][0]["text"])


async def _list_prompts(client: httpx.AsyncClient) -> list[dict]:
    """Send prompts/list and return the prompts array."""
    body = {
        "jsonrpc": "2.0",
        "id": _next_id(),
        "method": "prompts/list",
        "params": {},
    }
    headers = {**MCP_HEADERS, "X-User-Id": _E2E_USER}
    resp = await client.post("/mcp", json=body, headers=headers)
    resp.raise_for_status()
    data = resp.json()
    return data["result"]["prompts"]


async def _get_prompt(
    client: httpx.AsyncClient,
    name: str,
    arguments: dict,
) -> dict:
    """Send prompts/get and return the result."""
    body = {
        "jsonrpc": "2.0",
        "id": _next_id(),
        "method": "prompts/get",
        "params": {"name": name, "arguments": arguments},
    }
    headers = {**MCP_HEADERS, "X-User-Id": _E2E_USER}
    resp = await client.post("/mcp", json=body, headers=headers)
    resp.raise_for_status()
    data = resp.json()
    return data["result"]


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
def sqs() -> SQSClient:
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
            "task.create.v1",
            "task.list.v1",
            "task.status.v1",
            "task.cancel.v1",
            "task.list_actions.v1",
        }, f"expected 5 tools, got: {tool_names}"

        # ------------------------------------------------------------------
        # Step 2: task.create.v1 (immediate echo) → job_id + status "scheduled"
        # ------------------------------------------------------------------
        create_imm = await _mcp_call(
            client,
            "task.create.v1",
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

        status_1 = await _mcp_call(client, "task.status.v1", {"job_id": job_id_1})
        assert status_1["ok"] is True, f"step 3 status failed: {status_1}"
        assert status_1["data"]["status"] == "completed", (
            f"step 3: expected 'completed', got {status_1['data']['status']!r}"
        )

        # ------------------------------------------------------------------
        # Step 4: task.create.v1 (far-future one-shot) → job_id + "scheduled"
        # ------------------------------------------------------------------
        create_future = await _mcp_call(
            client,
            "task.create.v1",
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
        # Step 5: task.cancel.v1 → status "cancelled"
        # ------------------------------------------------------------------
        cancel = await _mcp_call(client, "task.cancel.v1", {"job_id": job_id_2})
        assert cancel["ok"] is True, f"step 5 cancel failed: {cancel}"
        assert cancel["data"]["status"] == "cancelled", (
            f"step 5: expected 'cancelled', got {cancel['data']['status']!r}"
        )

        # ------------------------------------------------------------------
        # Step 6: task.list.v1 → both jobs visible
        # ------------------------------------------------------------------
        list_result = await _mcp_call(client, "task.list.v1", {})
        assert list_result["ok"] is True, f"step 6 list failed: {list_result}"
        visible_ids = {j["job_id"] for j in list_result["data"]["jobs"]}
        assert job_id_1 in visible_ids, f"step 6: job_id_1={job_id_1} not in {visible_ids}"
        assert job_id_2 in visible_ids, f"step 6: job_id_2={job_id_2} not in {visible_ids}"


@pytest.mark.integration
async def test_w2_bonuses(mcp_client, session_factory, sqs):
    """W2 acceptance gate: steps 7-11 — recurring, cancel-recurring, chaining, resources, prompts."""  # noqa: E501
    async with mcp_client as client:
        # ------------------------------------------------------------------
        # Step 7: Recurring job — create with */1 * * * *, directly invoke the
        # watcher (stamp first run as SUCCEEDED so no SQS delay is needed),
        # tick RecurringJobWatcher, assert next JobRun spawned.
        #
        # "direct watcher invocation" per AC: rather than waiting for the cron
        # fire time, we stamp the first run terminal and call poll_once() to
        # verify the recurring spawn logic in CI without a real clock wait.
        # ------------------------------------------------------------------
        create_rec = await _mcp_call(
            client,
            "task.create.v1",
            {
                "action": "echo",
                "action_params": {"message": "recurring ping"},
                "schedule_type": "recurring",
                "cron_expr": "*/1 * * * *",
                "timezone": "UTC",
            },
        )
        assert create_rec["ok"] is True, f"step 7 create recurring failed: {create_rec}"
        rec_job_id: int = create_rec["data"]["job_id"]

        # Fetch the first (only) run and stamp it SUCCEEDED so the recurring
        # watcher can process the terminal event — bypassing real SQS delays.
        async with session_factory() as session:
            async with session.begin():
                run_row = (
                    (await session.execute(select(JobRun).where(JobRun.job_id == rec_job_id)))
                    .scalars()
                    .first()
                )
                assert run_row is not None, "step 7: first run should exist"
                run_row.status = "SUCCEEDED"
                session.add(
                    RunEvent(
                        run_id=run_row.run_id,
                        job_id=rec_job_id,
                        event_type="SUCCEEDED",
                        status_from="RUNNING",
                        status_to="SUCCEEDED",
                    )
                )

        # Recurring watcher sees the SUCCEEDED event and spawns the next run.
        spawned = await recurring_watcher_tick(session_factory)
        assert spawned >= 1, "step 7: recurring watcher should process at least one event"

        # Verify there is now a second PENDING run for the recurring job.
        async with session_factory() as session:
            runs = (
                (
                    await session.execute(
                        select(JobRun).where(JobRun.job_id == rec_job_id).order_by(JobRun.run_id)
                    )
                )
                .scalars()
                .all()
            )
        assert len(runs) >= 2, f"step 7: expected ≥2 runs for recurring job, got {len(runs)}"
        pending_runs = [r for r in runs if r.status == "PENDING"]
        assert len(pending_runs) >= 1, (
            f"step 7: expected a PENDING next run, statuses: {[r.status for r in runs]}"
        )

        # ------------------------------------------------------------------
        # Step 8: Cancel recurring — set cancelled_at; tick watcher again;
        # assert no further run is spawned.
        # ------------------------------------------------------------------
        cancel_rec = await _mcp_call(client, "task.cancel.v1", {"job_id": rec_job_id})
        assert cancel_rec["ok"] is True, f"step 8 cancel failed: {cancel_rec}"
        assert cancel_rec["data"]["status"] == "cancelled", (
            f"step 8: expected 'cancelled', got {cancel_rec['data']['status']!r}"
        )

        async with session_factory() as session:
            all_runs_before = (
                (await session.execute(select(JobRun).where(JobRun.job_id == rec_job_id)))
                .scalars()
                .all()
            )
        run_count_before = len(all_runs_before)

        # Tick the recurring watcher — it should see the CANCELLED event from
        # cancel but NOT spawn another run because cancelled_at is set.
        await recurring_watcher_tick(session_factory)

        async with session_factory() as session:
            all_runs_after = (
                (await session.execute(select(JobRun).where(JobRun.job_id == rec_job_id)))
                .scalars()
                .all()
            )
        assert len(all_runs_after) == run_count_before, (
            f"step 8: recurring watcher should not spawn after cancel "
            f"(before={run_count_before}, after={len(all_runs_after)})"
        )

        # ------------------------------------------------------------------
        # Step 9: Chaining — create A (immediate), create B triggered on A;
        # complete A; ChainWatcher flips B's run to PENDING; B completes.
        # ------------------------------------------------------------------
        create_a = await _mcp_call(
            client,
            "task.create.v1",
            {
                "action": "echo",
                "action_params": {"message": "chain upstream A"},
                "schedule_type": "immediate",
            },
        )
        assert create_a["ok"] is True, f"step 9 create A failed: {create_a}"
        job_a_id: int = create_a["data"]["job_id"]

        create_b = await _mcp_call(
            client,
            "task.create.v1",
            {
                "action": "echo",
                "action_params": {"message": "chain downstream B"},
                "schedule_type": "immediate",
                "trigger_on_job_id": job_a_id,
                "trigger_on_status": "SUCCEEDED",
            },
        )
        assert create_b["ok"] is True, f"step 9 create B failed: {create_b}"
        job_b_id: int = create_b["data"]["job_id"]

        # Drain any leftover SQS messages before claiming A's run.
        while True:
            leftover = sqs.receive_messages(max_messages=10, wait_seconds=0)
            if not leftover:
                break
            for msg in leftover:
                sqs.delete_message(msg["ReceiptHandle"])

        # Watcher claims A's PENDING run; worker executes A → SUCCEEDED.
        claimed_a = await claim_and_publish(session_factory, sqs)
        assert claimed_a >= 1, "step 9: watcher should claim A's run"

        msgs_a = sqs.receive_messages(max_messages=1, wait_seconds=2)
        assert len(msgs_a) == 1, "step 9: SQS should have A's message"
        await process_one(session_factory, sqs, msgs_a[0])

        # ChainWatcher sees A's SUCCEEDED event and flips B's WAITING run → PENDING.
        flipped = await chain_watcher_tick(session_factory)
        assert flipped >= 1, "step 9: chain watcher should flip at least one run"

        # Verify B's run is now PENDING.
        async with session_factory() as session:
            b_runs = (
                (await session.execute(select(JobRun).where(JobRun.job_id == job_b_id)))
                .scalars()
                .all()
            )
        assert any(r.status == "PENDING" for r in b_runs), (
            f"step 9: B's run should be PENDING, got: {[r.status for r in b_runs]}"
        )

        # Watcher claims B's PENDING run; worker executes B → SUCCEEDED.
        claimed_b = await claim_and_publish(session_factory, sqs)
        assert claimed_b >= 1, "step 9: watcher should claim B's run"

        msgs_b = sqs.receive_messages(max_messages=1, wait_seconds=2)
        assert len(msgs_b) == 1, "step 9: SQS should have B's message"
        await process_one(session_factory, sqs, msgs_b[0])

        status_b = await _mcp_call(client, "task.status.v1", {"job_id": job_b_id})
        assert status_b["ok"] is True, f"step 9 status B failed: {status_b}"
        assert status_b["data"]["status"] == "completed", (
            f"step 9: B should be 'completed', got {status_b['data']['status']!r}"
        )

        # ------------------------------------------------------------------
        # Step 10: Resources — resources/list (3 static) + templates/list (1)
        # = 4 total entries (3 static → 4 after W4-S09 added tasks://recent-results).
        # ------------------------------------------------------------------
        resources = await _list_resources(client)
        templates = await _list_resource_templates(client)
        total_resource_entries = len(resources) + len(templates)
        assert total_resource_entries == 4, (
            f"step 10: expected 4 resource entries (resources + templates), got "
            f"{len(resources)} resources + {len(templates)} templates = {total_resource_entries}"
        )
        resource_uris = {r["uri"] for r in resources}
        assert "tasks://list" in resource_uris, (
            f"step 10: tasks://list not in resources: {resource_uris}"
        )
        assert "tasks://actions" in resource_uris, (
            f"step 10: tasks://actions not in resources: {resource_uris}"
        )
        assert "tasks://recent-results" in resource_uris, (
            f"step 10: tasks://recent-results not in resources: {resource_uris}"
        )
        assert any("job" in t.get("uriTemplate", "") for t in templates), (
            f"step 10: job template missing from resourceTemplates: {templates}"
        )

        # tasks://list filters by user — only jobs created under _E2E_USER visible.
        list_content = await _read_resource(client, "tasks://list")
        assert "items" in list_content, f"step 10: tasks://list missing 'items': {list_content}"
        visible_job_ids = {item["job_id"] for item in list_content["items"]}
        assert job_a_id in visible_job_ids, (
            f"step 10: job A ({job_a_id}) should be in tasks://list snapshot"
        )
        assert job_b_id in visible_job_ids, (
            f"step 10: job B ({job_b_id}) should be in tasks://list snapshot"
        )

        # ------------------------------------------------------------------
        # Step 11: Prompts — prompts/list returns 2; prompts/get setup_summary
        # substitutes topic and schedule into the template.
        # ------------------------------------------------------------------
        prompts = await _list_prompts(client)
        assert len(prompts) == 2, (
            f"step 11: expected 2 prompts, got {len(prompts)}: {[p['name'] for p in prompts]}"
        )
        prompt_names = {p["name"] for p in prompts}
        assert "daily_review" in prompt_names, f"step 11: daily_review missing: {prompt_names}"
        assert "setup_summary" in prompt_names, f"step 11: setup_summary missing: {prompt_names}"

        prompt_result = await _get_prompt(
            client,
            "setup_summary",
            {"topic": "AI news", "schedule": "every morning at 8am"},
        )
        assert "messages" in prompt_result, (
            f"step 11: prompts/get result missing 'messages': {prompt_result}"
        )
        assert len(prompt_result["messages"]) >= 1, (
            "step 11: setup_summary should return at least one message"
        )
        message_text = prompt_result["messages"][0]["content"]["text"]
        assert "AI news" in message_text, (
            f"step 11: topic 'AI news' not substituted into prompt: {message_text!r}"
        )
        assert "every morning at 8am" in message_text, (
            f"step 11: schedule not substituted into prompt: {message_text!r}"
        )
