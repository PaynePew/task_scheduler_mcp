# W2 Manual Verification — MCP Inspector Click-Through Flow

Estimated time: ~5 minutes.  
Prerequisite: infrastructure running (`docker compose up -d postgres elasticmq`) and migrations applied (`alembic upgrade head`).

## Start the MCP server

```bash
MCP_USER_ID=local-dev MCP_USER_TZ=UTC npx @modelcontextprotocol/inspector \
  uv run python -m app.entrypoints.mcp_stdio
```

Open the browser URL printed by the inspector (usually `http://localhost:5173`). Click **Connect**.

---

## W1 steps (regression check — 6 steps)

### Step 1 — Connect: verify 5 tools

Click **Tools** in the inspector sidebar.

**Expected:** exactly 5 tools listed:
`task.create.v1`, `task.list.v1`, `task.status.v1`, `task.cancel.v1`, `task.list_actions.v1`

### Step 2 — Create an immediate echo task

Tool: **task.create.v1**
Arguments:
```json
{
  "action": "echo",
  "action_params": {"message": "hello from inspector"},
  "schedule_type": "immediate"
}
```

**Expected:** `{"ok": true, "data": {"job_id": <N>, "status": "scheduled"}}`

### Step 3 — Wait for completion

Wait ~10 seconds for the watcher + worker to process the job (requires `docker compose --profile full up` or the background processes running).

Tool: **task.status.v1** → `{"job_id": <N>}`

**Expected:** `"status": "completed"`

### Step 4 — Create a far-future one-shot task

Tool: **task.create.v1**
Arguments:
```json
{
  "action": "echo",
  "action_params": {"message": "far future"},
  "schedule_type": "one-shot",
  "scheduled_at": "2099-12-31T00:00:00+00:00"
}
```

**Expected:** `{"ok": true, "data": {"job_id": <M>, "status": "scheduled"}}`

### Step 5 — Cancel the future task

Tool: **task.cancel.v1** → `{"job_id": <M>}`

**Expected:** `{"ok": true, "data": {"status": "cancelled"}}`

### Step 6 — List all jobs

Tool: **task.list.v1** → `{}`

**Expected:** both job IDs visible in the `jobs` array.

---

## W2 steps (new capability checks — 5 steps)

### Step 7 — Create a recurring task

Tool: **task.create.v1**
Arguments:
```json
{
  "action": "echo",
  "action_params": {"message": "recurring ping"},
  "schedule_type": "recurring",
  "cron_expr": "@hourly",
  "timezone": "UTC"
}
```

**Expected:** `{"ok": true, "data": {"job_id": <R>, "status": "scheduled"}}`

Wait for the watcher to claim the first run (~5 s). Then check **task.status.v1** → `{"job_id": <R>}`.

**Expected:** after the first run completes, `status` becomes `"completed"` and the system schedules the next occurrence automatically (visible via `task.list.v1` showing `schedule_type: "recurring"`).

### Step 8 — Cancel the recurring task

Tool: **task.cancel.v1** → `{"job_id": <R>}`

**Expected:** `{"ok": true, "data": {"status": "cancelled"}}`

Subsequent watcher ticks will NOT spawn another run (the recurring watcher respects `cancelled_at`).

### Step 9 — Job chaining (A → B)

**Create job A (upstream):**

Tool: **task.create.v1**
```json
{
  "action": "echo",
  "action_params": {"message": "chain A"},
  "schedule_type": "immediate"
}
```
→ note `job_id_A`

**Create job B (downstream, triggered by A):**

Tool: **task.create.v1**
```json
{
  "action": "echo",
  "action_params": {"message": "chain B"},
  "schedule_type": "immediate",
  "trigger_on_job_id": <job_id_A>,
  "trigger_on_status": "SUCCEEDED"
}
```
→ note `job_id_B`

**Expected (immediately after creating B):**
- `task.status.v1` for B returns `"status": "scheduled"` (its run is in WAITING state internally).

Wait for job A to complete (~10 s with the full stack running). Then wait another ~5 s for the ChainWatcher to flip B's run to PENDING and the executor to complete B.

**Expected:** `task.status.v1` for B → `"status": "completed"`

### Step 10 — MCP Resources

Click **Resources** in the inspector sidebar.

**Expected:** 3 entries total:
1. `tasks://list` — "Task List" (static resource)
2. `tasks://actions` — "Action Registry" (static resource)
3. `tasks://job/{job_id}` — "Job Detail" (URI template)

Click **tasks://list** → **Read**.

**Expected:** JSON payload with `{"snapshot_at": "...", "total": <N>, "items": [...]}` containing only jobs owned by `MCP_USER_ID=local-dev`.

Click **tasks://actions** → **Read**.

**Expected:** JSON array listing `echo` and `http_call` actions with their `params_schema`.

### Step 11 — MCP Prompts

Click **Prompts** in the inspector sidebar.

**Expected:** 2 prompts listed:
1. `daily_review` — no required arguments
2. `setup_summary` — requires `topic` and `schedule`

Click **setup_summary** → fill:
- `topic`: `AI news`
- `schedule`: `every morning at 8am`

Click **Get Prompt**.

**Expected:** a user message containing both `"AI news"` and `"every morning at 8am"` substituted into the template, plus a reference to `tasks://actions`.

---

## Pass criteria

| Step | Check | Pass if |
|------|-------|---------|
| 1 | Tool count | Exactly 5 tools listed |
| 2 | Immediate create | `ok: true`, `status: "scheduled"` |
| 3 | Status after run | `status: "completed"` |
| 4 | One-shot create | `ok: true`, `status: "scheduled"` |
| 5 | Cancel | `status: "cancelled"` |
| 6 | List | Both job IDs visible |
| 7 | Recurring create | `ok: true`; next occurrence scheduled after first completes |
| 8 | Cancel recurring | `status: "cancelled"`; no further runs |
| 9 | Chain A→B | B completes after A succeeds |
| 10 | Resources | 3 entries; `tasks://list` filters by user |
| 11 | Prompts | 2 prompts; `setup_summary` substitutes args |
