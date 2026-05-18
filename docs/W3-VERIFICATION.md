# W3 Manual Verification — L4 Functional Smoke (Live Deployment)

**Target:** `https://scheduler.paynepew.dev`
**Estimated time:** ~8 minutes (5 min L4a wait + ~3 min steps)
**Prerequisite:** L1 (CI green on `main`), L2 (VPS provisioned), L3 (`/healthz` 200) already confirmed.

---

## Confirm L3 first

```bash
curl -fsS https://scheduler.paynepew.dev/healthz | jq
```

**Expected:**
```json
{"ok": true, "version": "<git-sha>", "db": "connected"}
```

If this returns 503 or times out, the deployment is down — do not proceed.

---

## How to send commands

Two equivalent paths — use whichever you prefer.

### Option A: MCP Inspector (recommended)

```bash
npx @modelcontextprotocol/inspector \
  --cli https://scheduler.paynepew.dev/mcp \
  --transport streamable-http \
  --header "X-User-Id: manual-l4-smoke"
```

Open the browser URL the inspector prints, click **Connect**, then use the
**Tools** panel to invoke each tool by name.

### Option B: curl

Set variables once, then paste the per-step commands:

```bash
export BASE="https://scheduler.paynepew.dev"
export UID_HEADER="X-User-Id: manual-l4-smoke-$$"
```

All curl snippets below POST JSON-RPC to `$BASE/mcp` with
`Accept: application/json` so the response is plain JSON rather than an SSE
stream. The tool result lives at `.result.content[0].text` (a JSON string).

---

## L4a — Echo Recurring Path

**Proves:** `mcp-server` + `watcher` + `worker` + `recurring_watcher` are alive and inter-communicating.

### Step A1 — Create recurring echo job

**MCP Inspector — Tool:** `task.create.v1`

```json
{
  "action": "echo",
  "action_params": {"message": "L4a"},
  "schedule_type": "recurring",
  "cron_expr": "* * * * *",
  "timezone": "UTC"
}
```

**curl:**

```bash
curl -s -X POST "$BASE/mcp" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json" \
  -H "$UID_HEADER" \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {
      "name": "task.create.v1",
      "arguments": {
        "action": "echo",
        "action_params": {"message": "L4a"},
        "schedule_type": "recurring",
        "cron_expr": "* * * * *",
        "timezone": "UTC"
      }
    }
  }' | jq '.result.content[0].text | fromjson'
```

**Expected response shape:**

```json
{
  "ok": true,
  "data": {
    "job_id": 42,
    "status": "scheduled"
  }
}
```

Note the returned `job_id` — referred to as `<JOB_RECURRING>` in steps below.

---

### Step A2 — Wait 5 minutes

The `* * * * *` cron fires at the start of every minute, so the
`recurring_watcher` will spawn the first run within ~60 s of job creation.
After 5 minutes at least 4 tick windows have passed; the `watcher` will have
claimed and the `worker` will have completed at least 2 runs.

```bash
echo "Waiting 5 minutes for ≥2 runs to complete…"; sleep 300
```

---

### Step A3 — Verify ≥ 2 completed runs

**MCP Inspector — Tool:** `task.status.v1`

```json
{
  "job_id": <JOB_RECURRING>,
  "include_runs": true
}
```

**curl:**

```bash
JOB_RECURRING=<paste-job_id-from-A1>
curl -s -X POST "$BASE/mcp" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json" \
  -H "$UID_HEADER" \
  -d "{
    \"jsonrpc\": \"2.0\",
    \"id\": 2,
    \"method\": \"tools/call\",
    \"params\": {
      \"name\": \"task.status.v1\",
      \"arguments\": {\"job_id\": $JOB_RECURRING, \"include_runs\": true}
    }
  }" | jq '.result.content[0].text | fromjson'
```

**Expected response shape:**

```json
{
  "ok": true,
  "data": {
    "job_id": 42,
    "action": "echo",
    "status": "scheduled",
    "runs": [
      {
        "run_id": 105,
        "status": "completed",
        "scheduled_at": "2026-05-18T12:03:00+00:00",
        "start_at": "2026-05-18T12:03:01.234567+00:00",
        "finish_at": "2026-05-18T12:03:01.456789+00:00"
      },
      {
        "run_id": 104,
        "status": "completed",
        "scheduled_at": "2026-05-18T12:02:00+00:00",
        "start_at": "2026-05-18T12:02:00.987654+00:00",
        "finish_at": "2026-05-18T12:02:01.123456+00:00"
      }
    ]
  }
}
```

**Pass criterion:** `data.runs` contains **at least 2 entries** where every listed
entry has `"status": "completed"`.

Note: the job-level `status` remains `"scheduled"` because the recurring job is
still active and pending future ticks. Run-level statuses are the evidence.

---

### Step A4 — Cleanup: cancel the recurring job

**MCP Inspector — Tool:** `task.cancel.v1`

```json
{"job_id": <JOB_RECURRING>}
```

**curl:**

```bash
curl -s -X POST "$BASE/mcp" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json" \
  -H "$UID_HEADER" \
  -d "{
    \"jsonrpc\": \"2.0\",
    \"id\": 3,
    \"method\": \"tools/call\",
    \"params\": {
      \"name\": \"task.cancel.v1\",
      \"arguments\": {\"job_id\": $JOB_RECURRING}
    }
  }" | jq '.result.content[0].text | fromjson'
```

**Expected:**

```json
{
  "ok": true,
  "data": {
    "job_id": 42,
    "status": "cancelled"
  }
}
```

---

## L4b — Chain A→B Path

**Proves:** `chain_watcher` is alive and flipping WAITING runs to PENDING when
their upstream dependency completes.

### Step B1 — Create job A (immediate echo)

**MCP Inspector — Tool:** `task.create.v1`

```json
{
  "action": "echo",
  "action_params": {"message": "A"},
  "schedule_type": "immediate"
}
```

**curl:**

```bash
curl -s -X POST "$BASE/mcp" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json" \
  -H "$UID_HEADER" \
  -d '{
    "jsonrpc": "2.0",
    "id": 10,
    "method": "tools/call",
    "params": {
      "name": "task.create.v1",
      "arguments": {
        "action": "echo",
        "action_params": {"message": "A"},
        "schedule_type": "immediate"
      }
    }
  }' | jq '.result.content[0].text | fromjson'
```

**Expected:**

```json
{
  "ok": true,
  "data": {
    "job_id": 43,
    "status": "scheduled"
  }
}
```

Note the `job_id` — referred to as `<JOB_A>`.

---

### Step B2 — Create job B (triggered by A succeeding)

**MCP Inspector — Tool:** `task.create.v1`

```json
{
  "action": "echo",
  "action_params": {"message": "B"},
  "schedule_type": "immediate",
  "trigger_on_job_id": <JOB_A>,
  "trigger_on_status": "SUCCEEDED"
}
```

**curl:**

```bash
JOB_A=<paste-job_id-from-B1>
curl -s -X POST "$BASE/mcp" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json" \
  -H "$UID_HEADER" \
  -d "{
    \"jsonrpc\": \"2.0\",
    \"id\": 11,
    \"method\": \"tools/call\",
    \"params\": {
      \"name\": \"task.create.v1\",
      \"arguments\": {
        \"action\": \"echo\",
        \"action_params\": {\"message\": \"B\"},
        \"schedule_type\": \"immediate\",
        \"trigger_on_job_id\": $JOB_A,
        \"trigger_on_status\": \"SUCCEEDED\"
      }
    }
  }" | jq '.result.content[0].text | fromjson'
```

**Expected:**

```json
{
  "ok": true,
  "data": {
    "job_id": 44,
    "status": "scheduled"
  }
}
```

Note the `job_id` — referred to as `<JOB_B>`.

At this point job B's run is internally in `WAITING` status, blocked on A.

---

### Step B3 — Wait 30 seconds

The watcher polls every 5 s; the echo action completes in < 1 s; `chain_watcher`
polls every 5 s. 30 s is comfortably sufficient for A to finish and B to be
unblocked, claimed, and completed.

```bash
echo "Waiting 30 seconds for chain to complete…"; sleep 30
```

---

### Step B4 — Verify both jobs completed

**MCP Inspector — Tool:** `task.status.v1`
Arguments: `{"job_id": <JOB_A>}`

**Expected:**

```json
{
  "ok": true,
  "data": {
    "job_id": 43,
    "action": "echo",
    "status": "completed"
  }
}
```

**MCP Inspector — Tool:** `task.status.v1`
Arguments: `{"job_id": <JOB_B>}`

**Expected:**

```json
{
  "ok": true,
  "data": {
    "job_id": 44,
    "action": "echo",
    "status": "completed"
  }
}
```

**curl — check A:**

```bash
JOB_A=<paste>
curl -s -X POST "$BASE/mcp" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json" \
  -H "$UID_HEADER" \
  -d "{
    \"jsonrpc\": \"2.0\",
    \"id\": 12,
    \"method\": \"tools/call\",
    \"params\": {\"name\": \"task.status.v1\", \"arguments\": {\"job_id\": $JOB_A}}
  }" | jq '.result.content[0].text | fromjson'
```

**curl — check B:**

```bash
JOB_B=<paste>
curl -s -X POST "$BASE/mcp" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json" \
  -H "$UID_HEADER" \
  -d "{
    \"jsonrpc\": \"2.0\",
    \"id\": 13,
    \"method\": \"tools/call\",
    \"params\": {\"name\": \"task.status.v1\", \"arguments\": {\"job_id\": $JOB_B}}
  }" | jq '.result.content[0].text | fromjson'
```

**Pass criterion:** both `<JOB_A>` and `<JOB_B>` return `"status": "completed"`.

---

### Step B5 — Cleanup: cancel both jobs

Per ADR-022, `task.cancel.v1` is idempotent against already-terminal jobs.
If all runs completed naturally, the server returns `INVALID_STATE` — that is
expected here and **not a failure**.

**MCP Inspector:**

Tool: `task.cancel.v1` → `{"job_id": <JOB_A>}`
Tool: `task.cancel.v1` → `{"job_id": <JOB_B>}`

**Expected (either acceptable):**

```json
{"ok": true, "data": {"job_id": 43, "status": "cancelled"}}
```

or

```json
{"ok": false, "error": {"code": "INVALID_STATE", "message": "Job 43 cannot be cancelled; current status is \"completed\""}}
```

---

## What failure means

| Symptom | Most likely cause | Where to look |
|---------|------------------|---------------|
| Step A1: `task.create.v1` returns `{"ok": false, ...}` or connection refused | `mcp-server` down or DB unreachable | `docker compose ps` on VPS; `/healthz` response |
| Step A3: `runs` array is empty after 5 min | `watcher` never claimed the run | `docker compose logs watcher` on VPS |
| Step A3: runs present but all `"status": "queued"` | `worker` dead or SQS unreachable | `docker compose logs worker` on VPS; check ElasticMQ |
| Step A3: fewer than 2 completed runs after 5 min | `recurring_watcher` not spawning next occurrences | `docker compose logs recurring_watcher` on VPS |
| Step B4: job A `"status": "completed"` but B still `"status": "scheduled"` | `chain_watcher` dead — B's run is stuck in `WAITING` | `docker compose logs chain_watcher` on VPS |
| Step B4: both A and B stuck in `"status": "scheduled"` | `watcher` or `worker` dead (same as L4a path) | Same as L4a row above |
| `/healthz` returns 503 | Postgres unreachable | `docker compose logs postgres` on VPS |

### Diagnostic commands (SSH to VPS)

```bash
# Check service health at a glance
docker compose ps

# Tail recent logs for any service
docker compose logs --tail=50 <service>
# <service>: mcp-server, watcher, worker, recurring_watcher, chain_watcher,
#            postgres, elasticmq, caddy

# Quick DB sanity check
docker compose exec postgres \
  psql -U scheduler -c \
  "SELECT status, count(*) FROM job_runs GROUP BY status ORDER BY count DESC;"
```

---

## Pass criteria summary

| Gate | Step | Check | Pass if |
|------|------|-------|---------|
| L4a | A1 | Recurring job created | `ok: true`, `status: "scheduled"` |
| L4a | A2 | Wait | (no check) |
| L4a | A3 | Completed runs | `include_runs` response contains **≥ 2** runs all with `"status": "completed"` |
| L4a | A4 | Cleanup | `ok: true`, `status: "cancelled"` |
| L4b | B1 | Job A created | `ok: true`, `status: "scheduled"` |
| L4b | B2 | Job B created (chained) | `ok: true`, `status: "scheduled"` |
| L4b | B3 | Wait | (no check) |
| L4b | B4 | Both completed | Both A and B return `"status": "completed"` |
| L4b | B5 | Cleanup | `ok: true` OR `INVALID_STATE` — both acceptable |

Both L4a **and** L4b must pass for W3 acceptance gate L4 to close.
