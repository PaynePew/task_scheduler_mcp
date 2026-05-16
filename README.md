# ChatGPT Task Scheduler

MCP-based job scheduler exposing **5 tools / 3 resources / 2 prompts** backed by Postgres + SQS (ElasticMQ locally).

Tools: `task.create.v1`, `task.list.v1`, `task.status.v1`, `task.cancel.v1`, `task.list_actions.v1`  
Resources: `tasks://list`, `tasks://actions`, `tasks://job/{job_id}` (template)  
Prompts: `daily_review`, `setup_summary`

W2 bonus features: recurring (cron) jobs · job chaining · cancel semantics · MCP resources + prompts. See [`docs/PRD/bonus-w2.md`](docs/PRD/bonus-w2.md) for the full W2 design.

## Quickstart

### Prerequisites

- [Docker](https://docs.docker.com/get-docker/) + Docker Compose
- [uv](https://docs.astral.sh/uv/getting-started/installation/) — `curl -LsSf https://astral.sh/uv/install.sh | sh`

### Setup

```bash
# 1. Install Python dependencies (creates .venv)
uv sync

# 2. Enable the local pre-commit hook (runs ruff check + format --check before each commit)
git config core.hooksPath .githooks

# 3. Copy the env-var template
cp .env.example .env
# Edit .env if you need non-default values (defaults work with docker compose)

# 4. Start infra (Postgres + ElasticMQ + one-shot migration)
docker compose up
```

Services:
| Service | URL |
|---------|-----|
| Postgres | `localhost:5432` |
| ElasticMQ (SQS API) | `http://localhost:9324` |
| ElasticMQ (stats UI) | `http://localhost:9325` |

### Run unit tests

```bash
uv run pytest -m "not integration"
```

### Run integration tests (requires running Compose services)

```bash
docker compose up -d postgres elasticmq
uv run pytest -m integration
```

### Lint + format check

```bash
uv run ruff check .
uv run ruff format --check .
```

### Full stack (all six entrypoints)

```bash
docker compose --profile full up
```

### Run E2E test (W1 + W2 acceptance gate)

The E2E test in `tests/integration/test_e2e_inspector_flow.py` covers the full
11-step acceptance gate: 6 W1 steps + 5 W2 steps (recurring, cancel-recurring,
chaining, resources, prompts). All steps run in-process via `httpx.ASGITransport`.

**Prerequisites:** the default Compose services must be running first.

```bash
# 1. Start infra + run migrations
docker compose up -d postgres elasticmq
alembic upgrade head          # or: docker compose run --rm migrate

# 2. Run only the E2E test
uv run pytest -m integration tests/integration/test_e2e_inspector_flow.py -v

# 3. Or run the full integration suite (includes E2E)
uv run pytest -m integration
```

## Project layout

```
app/
├── config/        # pydantic-settings; env-var contract
├── db/            # engine, session factory, ORM models, repositories
├── domain/        # business logic (stateless; no MCP awareness)
├── mcp/           # tool definitions, handlers, server wiring
├── workers/       # watcher / executor loops
├── queue/         # SQS / ElasticMQ client wrapper
├── actions/       # action handlers + registry
└── entrypoints/   # thin python -m targets (~10 lines each)
tests/
├── unit/          # fast, no external services
└── integration/   # require running Compose services
```

## Verify with the MCP Inspector

The MCP stdio server can be driven from the official inspector — no Claude client needed. Requires Node.js for `npx`.

```bash
MCP_USER_ID=local-dev MCP_USER_TZ=UTC npx @modelcontextprotocol/inspector \
  uv run python -m app.entrypoints.mcp_stdio
```

This opens a browser GUI (usually `http://localhost:5173`).

`MCP_USER_ID` is the tenant identifier the server scopes all jobs by (per ADR-006 / ADR-015).  
`MCP_USER_TZ` sets the default timezone for cron schedule expansion (ADR-017).

See [`docs/W2-VERIFICATION.md`](docs/W2-VERIFICATION.md) for the full 11-step click-through flow (W1 regression + W2 capability checks). Estimated time: ~5 minutes.

Expected counts in the inspector:
- **Tools tab:** 5 tools
- **Resources tab:** 3 entries (2 static resources + 1 URI template)
- **Prompts tab:** 2 prompts

## Verify with Claude Desktop (L3 sanity check)

Once the inspector tests pass, connect the server to Claude Desktop:

```bash
claude mcp add task-scheduler \
  -- uv run python -m app.entrypoints.mcp_stdio
```

Or add manually to `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS):

```json
{
  "mcpServers": {
    "task-scheduler": {
      "command": "/absolute/path/to/repo/.venv/bin/python",
      "args": ["-m", "app.entrypoints.mcp_stdio"],
      "cwd": "/absolute/path/to/repo",
      "env": {
        "MCP_USER_ID": "local-dev",
        "MCP_USER_TZ": "America/New_York"
      }
    }
  }
}
```

Restart Claude Desktop fully. The 🔨 icon in the chat input should show **5 tools**. The Resources and Prompts surfaces are discovered automatically by Claude.

Sample prompts to test:
> "Schedule an echo task to remind me about standup every weekday at 9am."  
> "What tasks do I have scheduled?"  
> "Cancel job #3."

## Bonus Challenges (all implemented in W2)

- **Connect a real LLM:** The MCP design itself is the NL parser surface — Claude Desktop / Claude Code IS the real LLM (see ADR-019)
- **Recurring jobs:** `schedule_type=recurring` with cron expressions or shortcuts (`@daily`, `@hourly`)
- **Job chaining:** `trigger_on_job_id` + `trigger_on_status` (SUCCEEDED | FAILED | ANY)
- **MCP resources:** `tasks://list`, `tasks://actions`, `tasks://job/{job_id}`
- **MCP prompts:** `daily_review`, `setup_summary`
