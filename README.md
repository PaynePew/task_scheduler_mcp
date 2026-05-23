# Task Scheduler MCP

**🌐 English** | [繁體中文](README.zh-TW.md)

A self-hostable MCP server that runs as a persistent HTTP service — **5 tools · 7 actions · 4 resources · 2 prompts** — so LLM clients can schedule, chain, and cancel recurring tasks backed by Postgres + SQS.

[![CI](https://github.com/PaynePew/task_scheduler_mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/PaynePew/task_scheduler_mcp/actions) [![Demo](https://img.shields.io/badge/demo-scheduler.paynepew.dev-blue)](https://scheduler.paynepew.dev) [![Status](https://img.shields.io/badge/status-status.paynepew.dev-green)](https://status.paynepew.dev)

---

## §1 Who this is for

Built for: developers who run their own webhooks/APIs and want to schedule them via natural-language LLM chat, with auditable persistence beyond chat sessions.

**Supported MCP clients:** Claude Desktop · Cursor · Claude in Chrome · MCP Inspector
**Not supported:** ChatGPT (Custom GPT Actions ≠ MCP protocol)

---

## §2 Architecture

```mermaid
flowchart LR
    User([User]) --> LLM[LLM Client<br/>Claude / Cursor]
    LLM -->|MCP tool call| MCP[MCP Server<br/>HTTP · stdio]
    MCP --> DB[(Postgres)]
    DB --> W[Watcher<br/>SKIP LOCKED]
    W -->|enqueue| Q[(SQS / ElasticMQ)]
    Q --> Worker[Worker]
    Worker -->|dispatches| AH[ActionHandler]
    AH -->|outbound API call| Ext[External Service<br/>Slack · GitHub · SMTP · R2 · ICS]
    Worker --> DB
    DB --> CW[ChainWatcher]
    DB --> RW[RecurringJobWatcher]
```

The MCP server persists `Job` + `JobRun` rows. The **Watcher** claims due runs via `FOR UPDATE SKIP LOCKED` and enqueues them. The **Worker** dispatches to one of 7 typed **ActionHandlers** (`echo` · `http_call` · `slack_post` · `github_digest` · `email_send` · `r2_upload` · `calendar_digest_ics`). **ChainWatcher** and **RecurringJobWatcher** consume the append-only `run_events` outbox — they never poll mutable state.

---

## §3 How to use

Three deployment paths. They use **different MCP transports** and **different OAuth scopes** — mixing them is the #1 setup pitfall. Pick one and follow it end-to-end.

|                          | **A. Hosted**                                      | **B. Self-host (HTTP)**                          | **C. Self-host (stdio)**                                    |
|--------------------------|----------------------------------------------------|--------------------------------------------------|-------------------------------------------------------------|
| MCP transport            | streamable-http over TLS                           | streamable-http                                  | stdio child process                                         |
| MCP endpoint             | `https://scheduler.paynepew.dev/mcp`               | `http://localhost:8000/mcp`                      | spawned: `uv run python -m app.entrypoints.mcp_stdio`       |
| `user_id` source         | WorkOS Bearer JWT (`sub` claim) — ADR-053          | `X-User-Id` header (trust-only) — ADR-015        | `MCP_USER_ID` env var (trust-only) — ADR-015                |
| OAuth dashboard          | `https://scheduler.paynepew.dev/connections`       | `http://localhost:8000/connections`              | `http://localhost:8000/connections` *(same web tier)*       |
| What to launch locally   | nothing                                            | `docker compose --profile full up -d`            | `docker compose --profile full up -d` *(for `/connections`)* + on-demand stdio spawn |
| `CONNECTIONS_BASE_URL`   | (operator-managed)                                 | `http://localhost:8000`                          | `http://localhost:8000`                                     |

> **Why this matters.** When an action (`github_digest`, `slack_post`, `email_send`) can't find an upstream OAuth token, the server replies with `MISSING_CONNECTION` + `connect_url` built from **its own `CONNECTIONS_BASE_URL`**. If you point your MCP client at Path A but try to OAuth on Path B/C (or vice versa), the two sides hold different `user_id`s, the connect_url shows the wrong host, and every action call silently fails.

### A. Hosted — zero install, try in 2 min

```jsonc
// Claude Desktop / Claude Code / Cursor MCP config
{ "mcpServers": { "task-scheduler": {
  "url": "https://scheduler.paynepew.dev/mcp",
  "transport": "streamable-http"
}}}
```

1. Restart your MCP client; the first tool call triggers the WorkOS OAuth flow ([RFC 9728 Protected Resource Metadata](https://www.rfc-editor.org/rfc/rfc9728)).
2. After signing in, open `https://scheduler.paynepew.dev/connections` and click Connect for each upstream provider you need (GitHub / Slack / Google).
3. Inspector quick-check: `curl https://scheduler.paynepew.dev/healthz` → `{"ok":true,"db":"connected"}`.

### B. Self-host (HTTP) — recommended for production

```bash
git clone https://github.com/PaynePew/task_scheduler_mcp
cd task_scheduler_mcp
cp .env.docker.example .env.docker     # ← compose reads THIS, not .env
cp .env.example .env                   # only for host-side `uv run` (tests, alembic)
docker compose --profile full up -d
```

```jsonc
// MCP client config (Claude Desktop / Code / Cursor)
{ "mcpServers": { "task-scheduler": {
  "url": "http://localhost:8000/mcp",
  "transport": "streamable-http",
  "headers": { "X-User-Id": "me" }
}}}
```

OAuth dashboard: open `http://localhost:8000/connections` → verify the page shows `Signed in as me` (matches your `X-User-Id` header) → click Connect for each provider. For an internet-facing deployment, edit `.env.docker`:

- Set `CONNECTIONS_BASE_URL=https://yourdomain.tld` so OAuth callback URLs and PRM resource URLs use the public host.
- Fill in §7 (WorkOS Bearer auth) — **never expose trust-only `X-User-Id` to the public internet** (anyone can read jobs by guessing the header).
- Run `bin/setup-vps.sh` on a fresh Ubuntu 24.04 box for Docker + Caddy + ufw + nightly Postgres backup + systemd auto-restart.

**BYO LLM via `http_call`:** set `action: "http_call"`, reference `${ANTHROPIC_API_KEY}` in headers/body — substituted at run time ([ADR-032](docs/adr/ADR-032-secrets-aware-action-handlers-and-env-var-substitution.md)); add the var name to `ALLOWED_TEMPLATE_VARS`. Rate limit: **1 000 creates/24h · 10/min burst** — env-configurable ([ADR-042](docs/adr/ADR-042-postgres-backed-rate-limiting.md)).

### C. Self-host (stdio) — MCP Inspector / dev convenience

Stdio MCP is a child process that dies with the chat ([§4](#§4-why-http-not-stdio) for why this is usually a bad idea). Use it only for MCP Inspector debugging or short-lived dev runs. **The OAuth dashboard still lives in the HTTP web tier**, so you still bring up the stack:

```bash
cp .env.docker.example .env.docker
cp .env.example .env
docker compose --profile full up -d    # web tier (for /connections) + Postgres + queue
```

```jsonc
// MCP client config — client spawns the stdio process on demand
{ "mcpServers": { "task-scheduler": {
  "type": "stdio",
  "command": "uv",
  "args": ["run", "python", "-m", "app.entrypoints.mcp_stdio"],
  "env": { "MCP_USER_ID": "me", "MCP_USER_TZ": "UTC" }
}}}
```

OAuth: open `http://localhost:8000/connections` → connect providers. Verify `Signed in as me` matches the `MCP_USER_ID` you passed to the stdio process.

> **The stdio gotcha.** Two processes read `MCP_USER_ID` from their own environment: the stdio process (from your MCP client's `env` block above) and the web tier (from `.env.docker`). They MUST resolve to the same string. If they differ, you OAuth as one user and the stdio process queries another — silent miss, error envelope returns `connect_url=http://localhost:8000/connections` even though the connection is already stored under a different user_id.

Inspector quick-check:

```bash
MCP_USER_ID=local-dev MCP_USER_TZ=UTC \
  npx @modelcontextprotocol/inspector uv run python -m app.entrypoints.mcp_stdio
```

Browse: [ADRs](docs/adr/) · [PRDs](docs/PRD/) · [Design Decisions](#design-decisions-adrs)

---

## §4 Why HTTP, not stdio

Stdio MCPs are child processes — they die when the chat closes. A scheduler must fire at wall-clock times regardless of which client is open. See [ADR-006](docs/adr/ADR-006-mcp-transport-dual-stdio-http.md).

---

## §5 Deployment Architecture

![Deployment architecture: VPS runtime + Fargate design artifact](docs/diagrams/d2-dual-deployment.png)

| | VPS (runtime — live) | Fargate (design artifact) |
|---|---|---|
| **Platform** | AWS Lightsail Tokyo | ECS Fargate / RDS / ALB / SQS |
| **Monthly cost** | ~$5 | ~$117–145 idle |
| **TLS** | Caddy auto-ACME | ACM + ALB |
| **Data** | Postgres in container + R2 backup | RDS Multi-AZ-ready |
| **Validated by** | Every CI/CD push | `validate-fargate.yml` (W4) |

---

## §6 Roadmap

| Gate | Description | Status |
|---|---|---|
| G1 | CI green; test coverage targets met | ✅ |
| G2 | 5 new handlers in registry; `task.list_actions.v1` returns 7 | ✅ |
| G3 | Digest workflow live — ≥ 5 consecutive Slack messages on production VPS | ✅ |
| G4 | `tasks://recent-results` queryable; returns real 24h data | ✅ |
| G5 | Landing page live — `curl https://scheduler.paynepew.dev/` returns 200 + HTML | ✅ |
| G6 | Rate limiting — integration test: 1001st create rejected | ✅ |
| G7 | Fargate evidence — dry + recording runs green; bill < $5 | ✅ |
| G8 | Visual artifacts — hero GIF + 4 screenshots + 3 diagrams in README | ✅ |
| G9 | README polished + i18n — EN + zh-TW, ~150 lines | ✅ |
| G10 | ADR cluster — 13 new W4 ADRs merged | ✅ |

---

## §7 Design Decisions (ADRs)

W1 scope + language + data store + queue + schema + module layout ([ADR-001–023](docs/adr/)):

| ADR | Decision |
|---|---|
| [ADR-001](docs/adr/ADR-001-project-scope.md) | Project scope |
| [ADR-002](docs/adr/ADR-002-implementation-language-python.md) | Python |
| [ADR-003](docs/adr/ADR-003-primary-data-store-postgres.md) | Postgres as primary store |
| [ADR-006](docs/adr/ADR-006-mcp-transport-dual-stdio-http.md) | Dual stdio + HTTP MCP transport |
| [ADR-007](docs/adr/ADR-007-watcher-ha-skip-locked.md) | Watcher HA via `SKIP LOCKED` |
| [ADR-008](docs/adr/ADR-008-message-queue-sqs.md) | SQS / ElasticMQ queue |
| [ADR-009](docs/adr/ADR-009-database-schema-outbox.md) | 3-table schema + transactional outbox |
| [ADR-013](docs/adr/ADR-013-action-catalog-typed-registry.md) | Typed action registry |
| [ADR-014](docs/adr/ADR-014-mcp-tool-surface-v1.md) | MCP tool surface v1 |
| [ADR-018](docs/adr/ADR-018-no-server-side-llm-in-w2.md) | No server-side LLM in W2 |
| [ADR-018-amended](docs/adr/ADR-018-amended-w4-reconsidered-stays-llm-agnostic.md) | W4 reconsidered — stays LLM-agnostic |

W3 deployment cohort ([ADR-024–031](docs/adr/)):

| ADR | Decision |
|---|---|
| [ADR-024](docs/adr/ADR-024-tier-scoping-and-w3-cut-scope.md) | W3 tier scoping |
| [ADR-025](docs/adr/ADR-025-network-topology-w3-public-ecs-private-rds.md) | Network topology |
| [ADR-026](docs/adr/ADR-026-ecs-service-topology-and-replica-count.md) | ECS service topology |
| [ADR-027](docs/adr/ADR-027-deployment-target-pivot-vps-first-aws-as-design-artifact.md) | VPS-first runtime; Fargate as design artifact |
| [ADR-028](docs/adr/ADR-028-caddy-over-nginx-for-vps-reverse-proxy.md) | Caddy over nginx |
| [ADR-029](docs/adr/ADR-029-vps-deployment-mechanics-ghcr-push-ssh-pull-containerized-data.md) | VPS deployment mechanics |
| [ADR-030](docs/adr/ADR-030-vps-operational-concerns-backup-monitoring-fargate-validation.md) | Operational concerns |
| [ADR-031](docs/adr/ADR-031-monitoring-better-stack-over-uptimerobot.md) | Better Stack monitoring |

W4 action sprint cohort:

| ADR | Decision |
|---|---|
| [ADR-032](docs/adr/ADR-032-secrets-aware-action-handlers-and-env-var-substitution.md) | Secrets via env-var substitution |
| [ADR-033](docs/adr/ADR-033-inter-handler-data-flow-via-job-run-result.md) | Inter-handler data plane via `JobRun.result` |
| [ADR-037](docs/adr/ADR-037-tasks-recent-results-mcp-resource-as-briefing-surface.md) | `tasks://recent-results` briefing surface |
| [ADR-038](docs/adr/ADR-038-mcp-call-as-future-direction.md) | Worker as MCP client *(Deferred — v2)* |
| [ADR-039](docs/adr/ADR-039-plan-abstraction-as-future-direction.md) | Plan abstraction *(Deferred — v2)* |
| [ADR-040](docs/adr/ADR-040-predicate-based-chain-as-future-direction.md) | Predicate-based chain *(Deferred — v2)* |
| [ADR-041](docs/adr/ADR-041-static-landing-page-and-caddy-path-routing.md) | Static landing page + Caddy path routing |
| [ADR-042](docs/adr/ADR-042-postgres-backed-rate-limiting.md) | Postgres-backed rate limiting |
| [ADR-044](docs/adr/ADR-044-project-rename-to-task-scheduler-mcp.md) | Project rename to `task_scheduler_mcp` |
| [ADR-045](docs/adr/ADR-045-email-send-action-design.md) | `email_send` SMTP action |
| [ADR-046](docs/adr/ADR-046-r2-upload-action-design.md) | `r2_upload` Cloudflare R2 / S3-compatible action |
| [ADR-048](docs/adr/ADR-048-calendar-digest-ics-action-design.md) | Calendar digest via signed ICS URL |

---

## §8 MCP Surface

**Tools (5):** `task.create.v1` · `task.list.v1` · `task.status.v1` · `task.cancel.v1` · `task.list_actions.v1`

**Actions (7):** `echo` · `http_call` · `slack_post` · `github_digest` · `email_send` · `r2_upload` · `calendar_digest_ics`

**Resources (4):** `tasks://list` · `tasks://actions` · `tasks://job/{job_id}` · `tasks://recent-results` *(24h result briefing)*

**Prompts (2):** `daily_review` · `setup_summary`

**Features:** cron recurrence · job chaining (`trigger_on_job_id`) · cancel semantics · `${VAR}` env-var substitution · rate limiting

---

## §9 Local Development

Host-side test loop (no full stack):

```bash
uv sync
cp .env.example .env
docker compose up -d postgres elasticmq             # Postgres + queue only
uv run alembic upgrade head
uv run pytest -m "not integration" && uv run pytest -m integration
uv run ruff check . && uv run ruff format --check .
```

For MCP Inspector against the stdio entrypoint, see [§3 Path C](#§3-how-to-use). Expected surface: **5 tools · 4 resources · 2 prompts** (W4 complete). Full click-through verification: [docs/W2-VERIFICATION.md](docs/W2-VERIFICATION.md).
