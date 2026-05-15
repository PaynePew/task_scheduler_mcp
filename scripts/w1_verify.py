"""W1 acceptance gate — programmatic equivalent of the 6-step MCP Inspector flow
from PROMPT.md § Verification, driven against the running Streamable HTTP server.

Run: uv run python scripts/w1_verify.py
"""

from __future__ import annotations

import asyncio
import json
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

SERVER_URL = "http://localhost:8000/mcp"
USER_ID = "w1-verify"
EXPECTED_TOOL_NAMES = {
    "task.create@v1",
    "task.list@v1",
    "task.status@v1",
    "task.cancel@v1",
    "task.list_actions@v1",
}


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, bool, str]] = []

    def record(self, step: str, ok: bool, detail: str = "") -> None:
        self.rows.append((step, ok, detail))
        marker = "PASS" if ok else "FAIL"
        print(f"  [{marker}] {step}{(' — ' + detail) if detail else ''}")

    def all_passed(self) -> bool:
        return all(ok for _, ok, _ in self.rows)

    def summary(self) -> str:
        passed = sum(1 for _, ok, _ in self.rows if ok)
        total = len(self.rows)
        return f"{passed}/{total} passed"


def _unwrap(call_result) -> dict:
    text = call_result.content[0].text
    return json.loads(text)


async def main() -> int:
    report = Report()

    async with streamablehttp_client(SERVER_URL, headers={"X-User-Id": USER_ID}) as (
        read_stream,
        write_stream,
        _get_session_id,
    ):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()

            # Step 1: list_tools — expect 5 tools (PRD says 5; PROMPT.md said 4 historically)
            print("\nStep 1: list_tools")
            tools_result = await session.list_tools()
            tool_names = {t.name for t in tools_result.tools}
            report.record(
                "list_tools returns 5 expected tools",
                tool_names == EXPECTED_TOOL_NAMES,
                f"got={sorted(tool_names)}",
            )

            # Step 2: task.create — immediate echo (semantically "past time" → runs ASAP)
            print("\nStep 2: task.create@v1 (immediate echo)")
            create_now = await session.call_tool(
                "task.create@v1",
                {
                    "action": "echo",
                    "action_params": {"message": "Summarize tech news"},
                    "schedule_type": "immediate",
                },
            )
            payload = _unwrap(create_now)
            ok = payload.get("ok") is True and "job_id" in payload.get("data", {})
            job_id_immediate = payload["data"]["job_id"] if ok else None
            report.record(
                "task.create@v1 immediate returns job_id + status=scheduled",
                ok and payload["data"].get("status") == "scheduled",
                f"job_id={job_id_immediate}",
            )

            # Step 3: wait ~12s then task.status — expect completed
            print("\nStep 3: wait 12s then task.status@v1")
            await asyncio.sleep(12)
            status_result = await session.call_tool(
                "task.status@v1",
                {"job_id": job_id_immediate},
            )
            status_payload = _unwrap(status_result)
            external_status = status_payload.get("data", {}).get("status")
            report.record(
                "immediate job transitions to 'completed' within 12s",
                status_payload.get("ok") is True and external_status == "completed",
                f"status={external_status}",
            )

            # Step 4: task.create — future-dated (one-shot 2099-12-31)
            print("\nStep 4: task.create@v1 (future one-shot)")
            future_iso = "2099-12-31T00:00:00+00:00"
            create_future = await session.call_tool(
                "task.create@v1",
                {
                    "action": "echo",
                    "action_params": {"message": "future"},
                    "schedule_type": "one-shot",
                    "scheduled_at": future_iso,
                    "timezone": "UTC",
                },
            )
            future_payload = _unwrap(create_future)
            ok = future_payload.get("ok") is True and "job_id" in future_payload.get("data", {})
            job_id_future = future_payload["data"]["job_id"] if ok else None
            report.record(
                "task.create@v1 future one-shot returns job_id",
                ok,
                f"job_id={job_id_future}",
            )

            # Step 5: task.cancel future job → status 'cancelled'
            print("\nStep 5: task.cancel@v1")
            cancel_result = await session.call_tool(
                "task.cancel@v1",
                {"job_id": job_id_future},
            )
            cancel_payload = _unwrap(cancel_result)
            report.record(
                "task.cancel@v1 returns ok=true",
                cancel_payload.get("ok") is True,
                f"data={cancel_payload.get('data')}",
            )

            status_after_cancel = await session.call_tool(
                "task.status@v1",
                {"job_id": job_id_future},
            )
            cancel_status_payload = _unwrap(status_after_cancel)
            external_cancel_status = cancel_status_payload.get("data", {}).get("status")
            report.record(
                "cancelled future job reports status='cancelled'",
                external_cancel_status == "cancelled",
                f"status={external_cancel_status}",
            )

            # Step 6: task.list — both jobs visible
            print("\nStep 6: task.list@v1")
            list_result = await session.call_tool("task.list@v1", {})
            list_payload = _unwrap(list_result)
            jobs = list_payload.get("data", {}).get("jobs", [])
            ids = {item.get("job_id") for item in jobs}
            report.record(
                "task.list@v1 returns both created jobs",
                {job_id_immediate, job_id_future}.issubset(ids),
                f"jobs={len(jobs)} ids={sorted(ids)}",
            )

    print("\n" + "=" * 60)
    print(f"W1 Inspector flow: {report.summary()}")
    print("=" * 60)
    if not report.all_passed():
        print("\nFailed rows:")
        for step, ok, detail in report.rows:
            if not ok:
                print(f"  - {step} ({detail})")
        return 1
    print("\n[OK] W1 acceptance gate PASSED -- all 6 verification steps green.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
