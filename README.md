# ChatGPT Task Scheduler

MCP-based job scheduler exposing `task.create / list / status / cancel / list_actions` tools backed by Postgres + SQS (ElasticMQ locally).

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

### Run E2E test (W1 acceptance gate)

The E2E test in `tests/integration/test_e2e_inspector_flow.py` reproduces the
6-step MCP Inspector verification flow from `PROMPT.md` § Verification step 2
against a real Postgres + ElasticMQ backend.

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

The test exercises all six steps in-process (no TCP port needed for the MCP
server): it uses `httpx.ASGITransport` for MCP tool calls, then drives
`claim_and_publish` (Watcher) and `process_one` (Worker) directly to complete
the immediate-echo job — the "CI equivalent" of the full `--profile full` stack.

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
MCP_USER_ID=local-dev npx @modelcontextprotocol/inspector uv run python -m app.entrypoints.mcp_stdio
```

This opens a browser GUI (usually `http://localhost:5173`). See `PROMPT.md` § Verification for the 6-step flow (create immediate → status completed → create future → cancel → list).

`MCP_USER_ID` is the tenant identifier the server scopes all jobs by (per ADR-006 / ADR-015).

## Bonus Challenges

- Connect a real LLM to parse natural language task descriptions before calling `task.create`
- Add recurring job support (cron expressions)
- Add job chaining (Job A completes -> triggers Job B)
- Add MCP `resources` support (e.g., expose job details as readable resources)
- Add MCP `prompts` support (e.g., a `daily_review` prompt template)
