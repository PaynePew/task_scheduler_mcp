#!/usr/bin/env python3
"""W3 L4 smoke test — verifies L4a (recurring echo) and L4b (chain A→B) against the live deployment.

Usage:
    uv run python bin/w3-smoke.py [--url URL] [--user-id UID]

Environment variables (overridden by flags):
    MCP_SERVER_URL   Base URL of the MCP server  (default: https://scheduler.paynepew.dev)
    MCP_USER_ID      X-User-Id header value       (default: smoke-l4-<pid>)

Exit codes:
    0 — both L4a and L4b passed
    1 — one or both gates failed (error detail printed to stdout)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

import httpx

DEFAULT_URL = "https://scheduler.paynepew.dev"
POLL_INTERVAL = 10  # seconds between status polls
L4A_TIMEOUT = 330  # 5.5 minutes: enough for 5+ cron ticks at * * * * *
L4B_TIMEOUT = 60  # 1 minute


def _call(
    client: httpx.Client,
    rpc_id: int,
    tool: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Send a tools/call JSON-RPC request and return the parsed tool result."""
    body = {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }
    resp = client.post("/mcp", json=body)
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"JSON-RPC error: {data['error']}")
    return json.loads(data["result"]["content"][0]["text"])


def _fmt(result: dict[str, Any]) -> str:
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# L4a
# ---------------------------------------------------------------------------


def run_l4a(client: httpx.Client, rpc_offset: int) -> tuple[bool, str]:
    """Run the echo-recurring smoke gate.

    Returns (passed: bool, detail: str).
    """
    print("\n=== L4a: Echo Recurring Path ===")

    # Step A1 — create recurring job
    create = _call(
        client,
        rpc_offset + 1,
        "task.create.v1",
        {
            "action": "echo",
            "action_params": {"message": "L4a"},
            "schedule_type": "recurring",
            "cron_expr": "* * * * *",
            "timezone": "UTC",
        },
    )
    if not create.get("ok"):
        return False, f"L4a A1 FAILED — task.create.v1 returned error:\n{_fmt(create)}"
    job_id: int = create["data"]["job_id"]
    print(f"  A1 ✓  created recurring job_id={job_id}, status={create['data']['status']!r}")

    # Step A2/A3 — poll until ≥ 2 completed runs
    print(f"  A2    polling for ≥2 completed runs (timeout {L4A_TIMEOUT}s) …")
    deadline = time.monotonic() + L4A_TIMEOUT
    completed_runs: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        time.sleep(POLL_INTERVAL)
        status = _call(
            client,
            rpc_offset + 2,
            "task.status.v1",
            {
                "job_id": job_id,
                "include_runs": True,
            },
        )
        if not status.get("ok"):
            print(f"    status poll error: {_fmt(status)}")
            continue
        runs = status["data"].get("runs", [])
        completed_runs = [r for r in runs if r.get("status") == "completed"]
        total_runs = len(runs)
        print(
            f"    … {total_runs} total run(s), {len(completed_runs)} completed"
            f" (elapsed {int(time.monotonic() - (deadline - L4A_TIMEOUT))}s)"
        )
        if len(completed_runs) >= 2:
            break
    else:
        # cleanup before failing
        _cancel_quietly(client, rpc_offset + 3, job_id, step="A4")
        return False, (
            f"L4a A3 FAILED — only {len(completed_runs)} completed run(s) "
            f"after {L4A_TIMEOUT}s (need ≥2). "
            "Check 'docker compose logs recurring_watcher watcher worker' on VPS."
        )

    print(f"  A3 ✓  {len(completed_runs)} completed runs confirmed")

    # Step A4 — cancel
    cancel = _call(client, rpc_offset + 4, "task.cancel.v1", {"job_id": job_id})
    if not cancel.get("ok"):
        return False, f"L4a A4 FAILED — cancel returned error:\n{_fmt(cancel)}"
    print(f"  A4 ✓  cancelled, status={cancel['data']['status']!r}")

    return True, f"L4a PASSED — {len(completed_runs)} completed runs for job_id={job_id}"


# ---------------------------------------------------------------------------
# L4b
# ---------------------------------------------------------------------------


def run_l4b(client: httpx.Client, rpc_offset: int) -> tuple[bool, str]:
    """Run the chain A→B smoke gate.

    Returns (passed: bool, detail: str).
    """
    print("\n=== L4b: Chain A→B Path ===")

    # Step B1 — create job A
    create_a = _call(
        client,
        rpc_offset + 1,
        "task.create.v1",
        {
            "action": "echo",
            "action_params": {"message": "A"},
            "schedule_type": "immediate",
        },
    )
    if not create_a.get("ok"):
        return False, f"L4b B1 FAILED — create A returned error:\n{_fmt(create_a)}"
    job_a: int = create_a["data"]["job_id"]
    print(f"  B1 ✓  created job A — job_id={job_a}, status={create_a['data']['status']!r}")

    # Step B2 — create job B chained on A
    create_b = _call(
        client,
        rpc_offset + 2,
        "task.create.v1",
        {
            "action": "echo",
            "action_params": {"message": "B"},
            "schedule_type": "immediate",
            "trigger_on_job_id": job_a,
            "trigger_on_status": "SUCCEEDED",
        },
    )
    if not create_b.get("ok"):
        return False, f"L4b B2 FAILED — create B returned error:\n{_fmt(create_b)}"
    job_b: int = create_b["data"]["job_id"]
    print(f"  B2 ✓  created job B — job_id={job_b}, status={create_b['data']['status']!r}")

    # Step B3/B4 — poll until both A and B are completed
    print(f"  B3    polling for both jobs completed (timeout {L4B_TIMEOUT}s) …")
    deadline = time.monotonic() + L4B_TIMEOUT
    status_a_val = status_b_val = "unknown"
    while time.monotonic() < deadline:
        time.sleep(POLL_INTERVAL)
        s_a = _call(client, rpc_offset + 3, "task.status.v1", {"job_id": job_a})
        s_b = _call(client, rpc_offset + 4, "task.status.v1", {"job_id": job_b})
        status_a_val = s_a.get("data", {}).get("status", "error") if s_a.get("ok") else "error"
        status_b_val = s_b.get("data", {}).get("status", "error") if s_b.get("ok") else "error"
        elapsed = int(time.monotonic() - (deadline - L4B_TIMEOUT))
        print(f"    … A={status_a_val!r} B={status_b_val!r} (elapsed {elapsed}s)")
        if status_a_val == "completed" and status_b_val == "completed":
            break
    else:
        _cancel_quietly(client, rpc_offset + 5, job_a, step="B5", label="A")
        _cancel_quietly(client, rpc_offset + 6, job_b, step="B5", label="B")
        hint = ""
        if status_b_val != "completed" and status_a_val == "completed":
            hint = " B did not complete after A — check 'docker compose logs chain_watcher'."
        elif status_a_val != "completed":
            hint = " A did not complete — check 'docker compose logs watcher worker'."
        return False, (
            f"L4b B4 FAILED — A={status_a_val!r} B={status_b_val!r} after {L4B_TIMEOUT}s.{hint}"
        )

    print(f"  B4 ✓  A={status_a_val!r}, B={status_b_val!r}")

    # Step B5 — cancel both. Naturally-terminated jobs return INVALID_STATE per ADR-022 § 3.
    for jid, label in [(job_a, "A"), (job_b, "B")]:
        _cancel_quietly(client, rpc_offset + 7, jid, step="B5", label=label)

    return True, f"L4b PASSED — chain A({job_a})→B({job_b}) both completed"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cancel_quietly(
    client: httpx.Client,
    rpc_id: int,
    job_id: int,
    *,
    step: str = "",
    label: str = "",
) -> None:
    """Cancel a job; swallow INVALID_STATE (already-terminal jobs per ADR-022)."""
    prefix = f"  {step} " if step else "  "
    try:
        result = _call(client, rpc_id, "task.cancel.v1", {"job_id": job_id})
        tag = f" {label}" if label else ""
        if result.get("ok"):
            print(f"{prefix}✓  cancelled job{tag} {job_id}")
        else:
            code = result.get("error", {}).get("code", "?")
            if code == "INVALID_STATE":
                print(f"{prefix}✓  job{tag} {job_id} already terminal (INVALID_STATE — expected)")
            else:
                print(f"{prefix}⚠  cancel job{tag} {job_id} returned: {result['error']}")
    except Exception as exc:
        print(f"{prefix}⚠  cancel job {job_id} raised {exc!r}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url",
        default=os.environ.get("MCP_SERVER_URL", DEFAULT_URL),
        help=f"MCP server base URL (default: {DEFAULT_URL})",
    )
    parser.add_argument(
        "--user-id",
        default=os.environ.get("MCP_USER_ID", f"smoke-l4-{os.getpid()}"),
        help="X-User-Id header (default: smoke-l4-<pid>)",
    )
    args = parser.parse_args()

    base_url: str = args.url.rstrip("/")
    user_id: str = args.user_id

    print(f"W3 L4 smoke — target: {base_url}  user: {user_id}")

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-User-Id": user_id,
    }

    with httpx.Client(base_url=base_url, headers=headers, timeout=30) as client:
        # Confirm healthz before running gates
        try:
            hz = client.get("/healthz")
            hz.raise_for_status()
            hz_data = hz.json()
            if not hz_data.get("ok"):
                print(f"BLOCKED — /healthz returned not-ok: {hz_data}")
                return 1
            print(f"  /healthz ✓  db={hz_data.get('db')!r} version={hz_data.get('version')!r}")
        except Exception as exc:
            print(f"BLOCKED — /healthz probe failed: {exc}")
            return 1

        start = time.monotonic()

        l4a_passed, l4a_detail = run_l4a(client, rpc_offset=0)
        l4b_passed, l4b_detail = run_l4b(client, rpc_offset=100)

        elapsed = time.monotonic() - start

    print(f"\n{'=' * 50}")
    print(f"Total elapsed: {elapsed:.0f}s")
    print(f"\nL4a: {'PASS' if l4a_passed else 'FAIL'}  {l4a_detail}")
    print(f"L4b: {'PASS' if l4b_passed else 'FAIL'}  {l4b_detail}")
    print(f"{'=' * 50}")

    if l4a_passed and l4b_passed:
        print("\nResult: ALL GATES PASSED — W3 L4 acceptance gate satisfied.")
        return 0
    else:
        print("\nResult: ONE OR MORE GATES FAILED — see details above.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
