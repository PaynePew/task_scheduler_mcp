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

# 2. Copy the env-var template
cp .env.example .env
# Edit .env if you need non-default values (defaults work with docker compose)

# 3. Start infra (Postgres + ElasticMQ + one-shot migration)
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

## How to Use

1. Read `PROMPT.md`
2. Answer the Design Questions (write your answers directly in `PROMPT.md`)
3. Build the prototype:
   - **Challenge Track:** Build from scratch using `PROMPT.md` as your spec
   - **Guided Track:** Go to `scaffold/`, fill in the TODOs
4. Verify with the MCP inspector tests at the bottom of `PROMPT.md`
5. Bring your Design Questions answers to live session for discussion

## Choose Your Track

**Challenge Track** — You decide the architecture, file structure, and implementation. Any language with an MCP SDK works (Python + the official `mcp` SDK recommended). Read `PROMPT.md` to get started.

**Guided Track** — File structure and boilerplate are provided. Fill in the core logic marked with `TODO`. Go to `scaffold/` and follow the instructions below.

## Guided Track Setup

```bash
cd scaffold
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

You also need **Node.js** for `npx` (used by the MCP inspector for verification).

### Files to Fill In

| File | TODO | Design Decision |
|------|------|-----------------|
| `app/scheduler.py` | `get_time_bucket()` + `find_due_jobs()` | Time bucket partitioning for efficient job scanning |
| `app/mcp_server.py` | `TOOL_REGISTRY` + `route_tool_call()` | Registry pattern for MCP tool routing |

### Run and Verify

The prototype is a real MCP stdio server. Verify with the MCP inspector (no Claude needed):

```bash
npx @modelcontextprotocol/inspector python -m app.mcp_server
```

This opens a browser GUI — see `PROMPT.md` Verification section for the full test flow. Once the inspector tests pass, you can optionally connect to Claude Desktop / Claude Code (instructions also in `PROMPT.md`).

## Bonus Challenges

- Connect a real LLM to parse natural language task descriptions before calling `task.create`
- Add recurring job support (cron expressions)
- Add job chaining (Job A completes -> triggers Job B)
- Add MCP `resources` support (e.g., expose job details as readable resources)
- Add MCP `prompts` support (e.g., a `daily_review` prompt template)
