# ChatGPT Task Scheduler

MCP-based job scheduler exposing **5 tools / 3 resources / 2 prompts** backed by Postgres + SQS (ElasticMQ locally).

## Live Demo

**[https://scheduler.paynepew.dev](https://scheduler.paynepew.dev)**

- `GET /healthz` — liveness check; returns HTTP 200 with DB connectivity confirmation
- MCP Inspector: `MCP_USER_ID=demo MCP_USER_TZ=UTC npx @modelcontextprotocol/inspector https://scheduler.paynepew.dev/mcp`

## Uptime Status

**[https://status.paynepew.dev](https://status.paynepew.dev)** — live uptime track record, powered by [Better Stack](https://betterstack.com) (monitoring vendor per [ADR-031](docs/adr/ADR-031-monitoring-better-stack-over-uptimerobot.md)). Checks `GET /healthz` every 3 minutes.

---

## Deployment Architecture

This project uses a **dual-target** deployment strategy that separates runtime from design artifact.

**Runtime target (live now):** AWS Lightsail Tokyo ($5/mo, 2 GB RAM / 1 vCPU). Caddy 2 terminates TLS; five Python services + Postgres + ElasticMQ run as Docker containers, matching the local dev Compose shape. Every push to `main` triggers a GitHub Actions deploy: build → push to `ghcr.io` → SSH pull → migrate → smoke `curl /healthz`.

**Design artifact (validated, not always-on):** AWS ECS Fargate / RDS PostgreSQL / ALB / SQS — fully Terraform-coded. Validated end-to-end via `validate-fargate.yml` (`workflow_dispatch`): apply → smoke → capture evidence → destroy. Bill < $5 per run, invoked once during W4 demo recording.

**Why dual?** Always-on Fargate costs $1,400–1,700/year — not viable during a job search. The VPS costs $60/year and keeps the demo live. The Fargate Terraform module is verifiable by anyone with AWS credentials; the architecture is a portfolio artifact, not a marketing claim.

```
VPS path (live)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Internet → Cloudflare DNS
         → Caddy 2 (auto-TLS, scheduler.paynepew.dev)
         → docker compose (AWS Lightsail Tokyo, ~$5/mo)
               ├── mcp-server   (HTTP + stdio MCP transports)
               ├── watcher      (FOR UPDATE SKIP LOCKED poll)
               ├── worker       (SQS consumer)
               ├── recurring_watcher
               └── chain_watcher
                   + Postgres 16 + ElasticMQ (SQS-compatible)

Fargate path (design artifact — Terraform-coded, validated once in W4)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Internet → ALB (HTTPS / ACM cert)
         → ECS Fargate tasks (same 5 services, ~$117/mo idle)
         → RDS PostgreSQL (private subnet)  + SQS (managed)
```

| | VPS (runtime) | Fargate (design artifact) |
|---|---|---|
| **Platform** | AWS Lightsail Tokyo | ECS Fargate / RDS / ALB / SQS |
| **Monthly cost** | ~$5 | ~$117–145 idle |
| **TLS** | Caddy auto-ACME | ACM + ALB |
| **Data** | Postgres in container + R2 backup | RDS Multi-AZ-ready |
| **Validated by** | Every CI/CD push | `validate-fargate.yml` (W4) |

---

## Roadmap

Parent PRD: [Issue #57 — W3 Deployment Surface](https://github.com/PaynePew/chatgpt_task/issues/57) · [`docs/PRD/deploy-w3.md`](docs/PRD/deploy-w3.md)

The 7-layer acceptance gate (per [`docs/PRD/deploy-w3.md` § D8](docs/PRD/deploy-w3.md); layering concept from [ADR-021](docs/adr/ADR-021-acceptance-gate-layering.md)):

| Layer | Description | Status |
|---|---|---|
| **L1** | Code green — all GH Actions workflows pass on `main`; `terraform plan` succeeds in PR | ✅ W3 |
| **L2** | Local provision — fresh Lightsail instance → all services healthy via `bin/setup-vps.sh` | ✅ W3 |
| **L3** | Live URL — `https://scheduler.paynepew.dev/healthz` returns 200 from external network; TLS valid | ✅ W3 |
| **L4a** | Echo recurring (`* * * * *`) → ≥ 2 completed JobRuns after 5 min | ✅ W3 |
| **L4b** | Chain A→B: both echo jobs complete; `chain_watcher` proved live | ✅ W3 |
| **L5** | Better Stack ≥ 24h green; R2 nightly backup present; restore drill passes | ✅ W3 |
| **L6** | Fargate evidence — `validate-fargate.yml` runs end-to-end; bill < $5 | ⬜ W4 |
| **L7** | Demo video — 3-minute portfolio demo against live VPS + Fargate apply | ⬜ W4 |

---

## Cost Transparency

Cost projection from [`docs/PRD/deploy-w3.md` § Cost projection](docs/PRD/deploy-w3.md):

| Phase | Monthly (USD) |
|---|---|
| W3 idle (Lightsail $5 + R2 free + Better Stack free + Cloudflare DNS free + domain ~$1 prorated) | **~$6** |
| Active deploys (GH Actions free on public repo; image pushes free) | $0 incremental |
| W4 one-shot Fargate validation | < $5 per run, one-time |
| **12-month projected (job search active)** | **~$70–80** |
| Comparison: original always-on Fargate plan | $1,400–1,700 |

---

## Design Decisions (ADRs)

All W3 design decisions are in [`docs/adr/`](docs/adr/). The W3 cohort (ADR-024 through ADR-031):

| ADR | Decision |
|---|---|
| [ADR-024](docs/adr/ADR-024-tier-scoping-and-w3-cut-scope.md) | W3 tier scoping — what ships vs what's deferred |
| [ADR-025](docs/adr/ADR-025-network-topology-w3-public-ecs-private-rds.md) | Network topology — public ECS tasks, private RDS, no NAT Gateway |
| [ADR-026](docs/adr/ADR-026-ecs-service-topology-and-replica-count.md) | ECS service topology — 5 services, fixed replicas, worker autoscaling |
| [ADR-027](docs/adr/ADR-027-deployment-target-pivot-vps-first-aws-as-design-artifact.md) | Deployment target pivot — VPS-first runtime, Fargate as design artifact |
| [ADR-028](docs/adr/ADR-028-caddy-over-nginx-for-vps-reverse-proxy.md) | Caddy 2 over nginx — cert rotation + MCP plugin ecosystem forward-compat |
| [ADR-029](docs/adr/ADR-029-vps-deployment-mechanics-ghcr-push-ssh-pull-containerized-data.md) | VPS deployment mechanics — build on GH Actions, push ghcr.io, SSH pull |
| [ADR-030](docs/adr/ADR-030-vps-operational-concerns-backup-monitoring-fargate-validation.md) | Operational concerns — R2 backup, monitoring, one-shot Fargate validation |
| [ADR-031](docs/adr/ADR-031-monitoring-better-stack-over-uptimerobot.md) | Monitoring vendor swap — Better Stack replaces UptimeRobot |

---

## Future Direction

### `(D-32)` Caddy MCP composability

The Caddy reverse proxy choice ([ADR-028](docs/adr/ADR-028-caddy-over-nginx-for-vps-reverse-proxy.md)) was made partly for forward-compatibility with the emerging Caddy MCP plugin ecosystem (`YawLabs/caddy-mcp`, `lum8rjack/caddy-mcp`). The `(D-32)` backlog item tracks the possibility of composing multiple MCP servers at the proxy layer — routing different MCP tool namespaces to different upstreams via Caddyfile — without application-layer changes. This positions the reverse proxy as a multi-MCP gateway rather than a simple pass-through.

---

## MCP surface

Tools: `task.create.v1`, `task.list.v1`, `task.status.v1`, `task.cancel.v1`, `task.list_actions.v1`  
Resources: `tasks://list`, `tasks://actions`, `tasks://job/{job_id}` (template)  
Prompts: `daily_review`, `setup_summary`

W2 bonus features: recurring (cron) jobs · job chaining · cancel semantics · MCP resources + prompts. See [`docs/PRD/bonus-w2.md`](docs/PRD/bonus-w2.md) for the full W2 design.

---

## Local Development

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
