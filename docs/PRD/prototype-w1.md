# PRD: ChatGPT Task Scheduler — W1 Prototype

> **Scope**: Local prototype (W1 of 4-week plan). Postgres + SQS-emulator + Docker Compose. **Not** in scope here: bonuses (W2), AWS deployment (W3), observability/CI/CD (W4) — see "Out of Scope" + companion docs in `.doc/learn/`.
>
> **Deliverable**: A runnable MCP-based job scheduler validated by the inspector flow in `PROMPT.md`.
>
> **Status**: Design 100% locked. Source decisions in `.doc/session/grilling-state.md` (Q1–Q15) + `.doc/learn/`.
>
> **Generated**: 2026-05-12 via `/to-prd` after Grilling Session #2.

---

## Problem Statement

LLM-powered chat clients (ChatGPT, Claude Desktop, Cursor) cannot natively schedule tasks for future or recurring execution. End users have to:
- Use generic schedulers (cron) — lose LLM context and natural-language UX.
- Use proprietary features (ChatGPT Tasks) — locked to one vendor and one client.
- Build custom integrations — re-implement reliable scheduling primitives every time (delivery guarantees, retries, hot-partition avoidance, recurring schedule expansion).

Operators wanting to expose a "schedule my task" capability across multiple LLM clients have no portable, multi-tenant primitive. They need a server that:
- Speaks a standardized protocol so any MCP-aware client can use it.
- Reliably executes scheduled work at scale (target NFRs: high availability, 10K req/sec scheduling peak, at-least-once execution).
- Lets developers add new task types (actions) without touching scheduling internals.

The W1 prototype is the smallest end-to-end slice that proves this primitive works locally and is shaped correctly for production (W3) without needing rewrites.

---

## Solution

A Python MCP server that exposes 5 tools (`task.create@v1`, `task.list@v1`, `task.status@v1`, `task.cancel@v1`, `task.list_actions@v1`) backed by:

1. **A Job Scheduling layer** that persists `Job` definitions and `JobRun` instances into Postgres with a `RunEvent` append-only outbox.
2. **A Watcher process** that scans for due `JobRun`s within a 5-minute lookahead window and publishes them to a queue (SQS in production, ElasticMQ locally).
3. **A Worker process** that pulls from the queue, claims runs atomically (defending against duplicate execution), and dispatches to the correct **Action handler** (typed registry of executable task types).
4. **A RecurringJobWatcher** subscribing to terminal `RunEvent`s to spawn the next instance of recurring jobs.
5. **A ChainWatcher** subscribing to terminal `RunEvent`s to flip `WAITING` runs (whose parent run completed) into `PENDING`.
6. **Dual MCP transport**: stdio for desktop integration (Claude Desktop, MCP Inspector) and Streamable HTTP for cloud-style demo on the same codebase.

Locally: `docker compose up` brings the entire stack online. Inspector tests in `PROMPT.md` validate the user-facing flows.

---

## User Stories

### End-user (LLM-mediated) scheduling

1. As an LLM client, I want to schedule an **immediate** task, so that the action runs as soon as a worker picks it up.
2. As an LLM client, I want to schedule a **one-shot** task at a specific future ISO 8601 datetime, so that work happens at the user's intended moment.
3. As an LLM client, I want to schedule a **recurring** task with a cron expression, so that the user can express "every day at 9am" without re-creating tasks. *(Schema supports it in W1; cron expansion logic ships in W2.)*
4. As an LLM client, I want to specify a **timezone** for scheduled times, so that "9am" matches the user's locale rather than UTC.
5. As an LLM client, I want to pass an **idempotency_key** when creating a task, so that retries from my side never create duplicate jobs.
6. As an LLM client, I want to **discover available actions** with their parameter schemas, so that I can construct valid task params on the first try.
7. As an LLM client, I want every tool error to follow a **structured shape** (`{ok: false, error: {code, message, field, expected}}`), so that I can self-correct and retry.

### End-user (LLM-mediated) inspection

8. As an LLM client, I want to **list the user's tasks** newest-first, so that the user can browse what they have scheduled.
9. As an LLM client, I want to **filter the list by status** ("scheduled" / "running" / "completed" / "failed" / "cancelled"), so that I can surface only what the user asked for.
10. As an LLM client, I want to **filter by created_at range**, so that I can answer "show tasks I made yesterday".
11. As an LLM client, I want **offset-based pagination** (`page` + `pageSize`), so that I can browse long histories.
12. As an LLM client, I want to **fetch one task in detail** by `job_id` (with optional execution history for recurring jobs), so that I can show the user the latest result.
13. As an LLM client, I want to **cancel** a task that hasn't completed, so that the user can withdraw work.
14. As an LLM client, I want to receive a clear `INVALID_STATE` error when cancelling a `completed` or `failed` task, so that I can explain "already finished" rather than silently failing.

### Action execution

15. As a worker, I want to dispatch each `JobRun` to the registered Action handler keyed by `Job.action`, so that adding a new action requires no changes to dispatching code.
16. As an action handler author, I want to declare a **Pydantic params model**, **timeout**, and return an `ActionResult` with `ok` / `result` / `retryable`, so that retry policy is data-driven and per-action.
17. As a worker, I want to enforce per-action **timeout** with `asyncio.wait_for`, so that a hung action becomes a retryable failure instead of blocking the worker forever.
18. As a worker, I want to extend SQS **visibility timeout via heartbeat** every 30 seconds while a long action runs, so that the message isn't redelivered prematurely.
19. As a worker, I want **claim-and-mark** semantics (`UPDATE ... WHERE status IN ('PENDING','QUEUED') RETURNING ...`), so that two workers receiving the same redelivered message do not both execute.
20. As a worker, I want every status transition to write a `RunEvent` in the **same transaction**, so that downstream reactors never see status without a corresponding event (or vice versa).

### Reliability and scale shape

21. As an operator, I want **multiple watcher processes** to run concurrently with `FOR UPDATE SKIP LOCKED` claims, so that watcher availability does not depend on a single instance and we never need leader election.
22. As an operator, I want **failed messages to land in a DLQ** after `max_retries` attempts, so that pathological jobs don't churn the queue forever.
23. As an operator, I want each **Postgres connection pool sized per process role**, so that mcp-server / watcher / worker do not collectively exhaust RDS `max_connections`.
24. As an operator, I want `pool_pre_ping` and `pool_recycle=3600` configured by default, so that stale connections discovered after long idle periods are recycled rather than crashing requests.
25. As an operator, I want **time-bucket partitioning of `job_runs`**, so that watcher scans are O(log n) within the current hour rather than O(n) over all-time history. *(Implemented as column + composite index in W1; upgraded to declarative `PARTITION BY RANGE` in W2.)*
26. As an operator, I want **terminal `RunEvent` outbox semantics**, so that RecurringJobWatcher and ChainWatcher react to immutable events instead of polling mutable state — eliminating a class of race conditions.

### Recurring schedules and chaining

27. As an LLM client, I want to schedule a recurring `Job` with a cron expression and have the system **automatically create the next `JobRun`** after the previous one terminates, so that the user does not re-schedule daily. *(Scaffold + schema in W1; cron expansion logic ships in W2.)*
28. As an LLM client, I want to **link a job to wait for another job's terminal status** (`SUCCEEDED` / `FAILED` / `ANY`), so that I can compose simple linear chains. *(Schema in W1; ChainWatcher logic ships in W2.)*

### Multi-tenancy

29. As an operator running stdio mode (Claude Desktop), I want a single `MCP_USER_ID` env var to identify all jobs from this client, so that desktop integration needs no extra setup.
30. As an operator running HTTP mode for a multi-user demo, I want `X-User-Id` HTTP header to scope jobs per request, so that I can show two users not seeing each other's tasks. *(Header is trust-only in W1 — only safe behind a real auth proxy in W3.)*
31. As an operator, I want jobs unambiguously associated with a `user_id` from day 1, so that future ALB OIDC integration in W3 only needs to swap the resolver function.

### Developer experience

32. As a developer, I want to add a new MCP tool by editing only three files (handler, tool definition, registry entry), so that contributing tools is a one-line dispatch change.
33. As a developer, I want to add a new action by adding one Pydantic model + one handler class + one registry entry, so that the workers automatically dispatch to it.
34. As a developer, I want **`docker compose up` (infra-only profile)** to start Postgres + ElasticMQ + automatically run pending Alembic migrations, so that I can begin coding within seconds.
35. As a developer, I want **`docker compose --profile full up`** to additionally start mcp-server / watcher / worker / recurring-watcher / chain-watcher, so that I can run the entire stack in containers for integration testing.
36. As a developer, I want **handlers to be pure functions on `(db, args)`** independent of MCP transport, so that I can unit-test them without spawning the MCP server.
37. As a developer, I want the **same Docker image** to serve all 6 entrypoints via different `command:` values, so that I maintain one Dockerfile and one dependency graph.
38. As a developer, I want to **switch MCP transport via `--transport stdio|http`** at the entrypoint, so that the same business code serves desktop and cloud.
39. As a developer, I want **type-checked configuration via pydantic-settings** with `.env.example` committed and `.env` gitignored, so that env requirements are discoverable and secrets aren't leaked.

### Future-proofing for resume-narrative

40. As a portfolio author, I want each technical choice traceable to a documented trade-off (in `course-spec.md` § 10 / `interview-questions.md`), so that I can defend any choice for 10+ minutes in interview.
41. As a portfolio author, I want the architecture's physical layout (6 entrypoints) to map 1:1 to a future ECS task definition layout (W3), so that "lift-and-shift to AWS" is a true 1-week task and not a rewrite.

---

## Implementation Decisions

### D1. Architecture topology (Q5, Q5b, Q7)

The system is six logical processes, all sharing one Python codebase and one Docker image, started via different `python -m` entrypoints:

1. **mcp-server** — handles MCP requests over stdio or Streamable HTTP. Front door in W3 is **ALB** (not API Gateway — API Gateway HTTP API has a 30s idle timeout that cuts SSE connections; full reasoning in `aws-deep-dive.md` § 1).
2. **mcp-watcher** (×2–3 in W3) — scans `job_runs` for due work in the next 5 minutes and pushes to SQS. Multiple watchers run safely concurrent via `FOR UPDATE SKIP LOCKED` (no leader election).
3. **mcp-worker** (×N in W3) — pulls SQS messages, claims runs atomically, dispatches to action handlers, writes status + outbox event in a single transaction.
4. **recurring-watcher** (×1) — polls `run_events` for terminal events of recurring jobs and inserts the next `JobRun`.
5. **chain-watcher** (×1) — polls `run_events` for terminal events; for any `WAITING` run whose `wait_for_run_id` matches, flips it to `PENDING` (or `CANCELLED` if `trigger_on_status` doesn't match).
6. **migrate** (one-shot, non-restart) — runs `alembic upgrade head` before app services start.

W1 runs all six locally via Docker Compose. W3 each becomes its own ECS Fargate service definition.

### D2. Data model (Q9, Q13)

Three tables — full schema in `.doc/learn/system-design.md` § 7.4. Concept summary:

- **`jobs`** — mutable definition. Includes `user_id`, `description`, `action`, `action_params (JSONB)`, scheduling fields (`scheduled_at` for one-shot, `cron_expr` for recurring, `timezone`), `idempotency_key UNIQUE`, chaining fields (`trigger_on_job_id`, `trigger_on_status`), LLM-bonus fields (`raw_user_input`, `parsing_metadata`).
- **`job_runs`** — execution instance. Partition key column `time_bucket` (W1 indexed; W2 upgrades to native `PARTITION BY RANGE`). Composite PK `(time_bucket, run_id)`. Includes lifecycle status, retry counters, `wait_for_run_id` (chaining), audit timestamps. Partial index on `(time_bucket, scheduled_at) WHERE status IN ('PENDING','WAITING')` is the watcher's hot query.
- **`run_events`** — append-only outbox. Stores transition events (`CREATED` / `QUEUED` / `STARTED` / `SUCCEEDED` / `FAILED` / `RETRY` / `CANCELLED`) with `processed_by` JSONB tracking which downstream watcher consumed each event.

Two access-pattern indexes deserve special note:
- `idx_jobs_user_created (user_id, created_at DESC)` — serves `task.list@v1` (per the course's "user lists own jobs" pattern; in DDB this would be the GSI with PK=user_id SK=created_at).
- `idx_job_runs_due (time_bucket, scheduled_at)` partial — serves the watcher.

We deliberately do **not** physically partition `jobs` by `user_id`. At prototype scale, an index achieves the per-user query locality without paying partition migration costs. (Interview answer for "why not?" in `interview-questions.md` B8.)

### D3. Status state machine (Q9, Q14)

Internally **8 statuses**: `PENDING` → `QUEUED` → `RUNNING` → (`SUCCEEDED` | `FAILED`); `RETRYING` is a transient sub-state of running; `CANCELLED` and `WAITING` are independent.

Externally (what MCP tools return) **5 statuses**: `scheduled` (covers PENDING/QUEUED/WAITING), `running` (covers RUNNING/RETRYING), `completed` (SUCCEEDED), `failed`, `cancelled`. Mapping is done at the handler boundary — internal precision preserved in DB, external simplicity for LLM consumption.

### D4. MCP tool surface (Q14)

Five `@v1`-versioned tools. **Versioning suffix is mandatory** because MCP clients cache `tools/list` per thread; changing a tool's schema mid-thread breaks long conversations. To evolve a tool, ship `task.foo@v2` alongside `@v1`.

Every tool input schema specifies `required`, `enum`, `default`, and `additionalProperties: false` per the course's reliability principles (`course-spec.md` § 7).

Every tool returns the **envelope**:

```
Success: {"ok": true, "data": {...}}
Failure: {"ok": false, "error": {"code", "message", "field", "expected"}}
```

with `code` drawn from a fixed vocabulary: `USER_INPUT`, `NOT_FOUND`, `INVALID_STATE`, `UNKNOWN_ACTION`, `DUPLICATE`, `INTERNAL`.

The complete inputSchema for each tool is locked in `.doc/learn/system-design.md` § 7.4 and re-stated in `course-spec.md`. The five tools are:

- **`task.create@v1`** — creates a `Job` (and an initial `JobRun` for one-shot/immediate).
- **`task.list@v1`** — returns the user's jobs newest-first with optional status filter, time range, and offset pagination.
- **`task.status@v1`** — returns one job; with `include_runs=true` includes recent execution history.
- **`task.cancel@v1`** — flips eligible jobs to `CANCELLED`, errors with `INVALID_STATE` for terminal jobs.
- **`task.list_actions@v1`** — returns the action registry: name, description, timeout, JSON Schema for params. The LLM is instructed to call this once per thread to learn parameter shapes.

A `~125`-token system instruction guides the LLM to call `task.list_actions@v1` once at the start of a thread and to default timezone to UTC + schedule_type to `immediate` when the user is silent — full text in `.doc/learn/course-spec.md` § 7.4.

### D5. Action handler protocol (Q13)

A worker dispatches based on `Job.action`. Each action is a handler conforming to the protocol below — included verbatim because it encodes the type shape more precisely than prose:

```python
class ActionResult:
    ok: bool
    result: dict | None        # serialized to job_runs.result
    error: str | None
    retryable: bool = True     # False → permanent failure → DLQ

class ActionHandler(Protocol):
    name: ClassVar[str]
    params_model: ClassVar[type[BaseModel]]   # Pydantic model for action_params
    timeout_seconds: ClassVar[int]            # asyncio.wait_for budget

    async def execute(self, run: JobRun, params: BaseModel) -> ActionResult: ...
```

W1 ships **two** handlers:
- `echo` — returns `{"echoed": <message>}`. Smoke-test of the entire pipeline.
- `http_call` — `httpx.AsyncClient` request with method/headers/body params. Result body truncated to 2KB to avoid bloating `job_runs.result`. `retryable` set to `(status_code >= 500)` so we retry server errors but not user errors.

W2 will add `llm_summarize`, `llm_chat`, `send_email` (registry pattern means no dispatcher changes). Action enum is referenced directly from `ACTION_REGISTRY.keys()` in the `task.create@v1` schema and exposed via `task.list_actions@v1`.

### D6. Transport (Q6)

The MCP server module is transport-agnostic. Two entrypoints select the transport:

- **stdio entrypoint** — for Claude Desktop, `npx @modelcontextprotocol/inspector`, and any subprocess-launched client. Reads `MCP_USER_ID` env var for tenant identity.
- **HTTP entrypoint** — wraps the same server in `streamable_http_server`, listens on `$PORT`. Reads optional `X-User-Id` header (falling back to env var, then `default-user`).

This dual-target design means the **same codebase serves Claude Desktop locally and ECS Fargate behind ALB in production** — a key resume-narrative point.

### D7. Persistence layer (Q11)

- **Async SQLAlchemy 2.0 + asyncpg** for runtime. Avoids `asyncio.to_thread` bridging that the scaffold uses; matches the MCP SDK's async-native nature.
- **Sync psycopg URL for Alembic** migrations only. Async + Alembic is rough-edged; sync is rock-solid.
- **`expire_on_commit=False`** so committed objects can still be serialized into the response (otherwise lazy-load triggers on a closed session → `DetachedInstanceError`).
- **Per-request session** (MCP handlers) and **per-iteration session** (worker loops) — never long-lived. Sessions check out a connection from the pool and return it on `commit/rollback/close`. Rationale and pitfalls in `interview-questions.md` H6–H12.
- **Connection pool sized per process role**: mcp-server (5+10), watcher (2+3), worker (5+10), recurring/chain watchers (2+3 each). Total max 130 connections across the W3 footprint — exceeds RDS db.t4g.micro `max_connections=81`, so W3 either tunes pools down or upgrades to db.t4g.small (or adds RDS Proxy). W1 is unaffected.
- **`pool_pre_ping=True` + `pool_recycle=3600`** non-negotiable to survive idle disconnects.

### D8. Queue and at-least-once semantics (Q8, Q13)

- **AWS SQS** in production; **ElasticMQ** locally (single Docker container, SQS-compatible API).
- Watcher's lookahead is **5 minutes**; matching SQS `DelaySeconds` is set so the worker only sees a message at its `scheduled_at` (SQS supports up to 15 min delay; 5 fits).
- Worker initial **visibility timeout = 60s**; **heartbeat every 30s** extends visibility by 60s while the action runs. On crash/timeout the message becomes visible again.
- **Max receive count = 3**, then the message routes to a **DLQ** — set up in SQS console / IaC, no app code required.
- **Idempotency at two levels**:
  - DB layer: `UPDATE job_runs SET status='RUNNING' WHERE run_id=:rid AND status IN ('PENDING','QUEUED') RETURNING ...` is the atomic "claim" — only one worker wins.
  - Action layer: each handler is responsible for being idempotent on the underlying side-effect (e.g., HTTP idempotency keys, email dedup).

### D9. Outbox pattern (Q9, Q15-related)

Every status transition writes a `RunEvent` in the **same transaction** as the `job_runs.status` update. RecurringJobWatcher and ChainWatcher consume `run_events` (immutable history) instead of polling `job_runs.status` (mutable). This eliminates the race where:
- Watcher A reads "status = SUCCEEDED" at T1.
- Worker B at T2 retries the same job (because of duplicate delivery) and updates status briefly to RUNNING then back to SUCCEEDED.
- Watcher A would have created two next-instances naively.

With outbox, both watchers read the immutable event stream with `event_id` cursors and can deduplicate trivially. This is the **transactional outbox pattern** straight from `course-spec.md` and the canonical solution.

### D10. Multi-tenant identity (Q15)

A single resolver function determines `user_id` per request:

1. If MCP request carries `X-User-Id` HTTP header → use it.
2. Else if `MCP_USER_ID` env var is set → use it.
3. Else → `"default-user"`.

In W1 this is **trust-only** — there is no auth on the header. The W1 deployment surface is local only. W3 plan: ALB OIDC authentication injects `x-amzn-oidc-identity` (Cognito sub claim) and the resolver swaps to read that header instead. The schema column `user_id TEXT NOT NULL` is format-agnostic, so no migration required when the source changes.

### D11. Module layout (Q10)

A layered structure under a single `app/` package with these conceptual modules (specific filenames in `system-design.md` § 7 and Q10 of grilling-state):

- **config** — pydantic-settings reading `.env`. Single source of truth for env vars.
- **db** — engine, async session factory, ORM models, repository functions (pure DB queries).
- **domain** — business logic (create_job, claim_run, complete_run, fail_run). Knows nothing about MCP.
- **mcp** — tool definitions, handlers, registry, error formatter, server wiring. Handlers wrap domain calls + MCP serialization.
- **workers** — watcher / executor / recurring_watcher / chain_watcher loops.
- **queue** — SQS client wrapper (send/receive/delete/visibility/heartbeat).
- **actions** — action handlers + registry.
- **entrypoints** — six `python -m`-targets, each ~10 lines tying transport/loop to the right module.

This layout maps 1:1 to W3's six ECS service definitions and to the architecture diagram readers see in the README.

### D12. Tooling (Q12)

- **Python 3.12+**, dependencies via **`uv`** (Astral's Rust-based package manager — 10-100× faster than pip; modern signal in `pyproject.toml`).
- **Alembic** for migrations. `service_completed_successfully` Compose dependency means migrations always apply before app services start; no manual step.
- **Docker Compose with two profiles**: default = infra only (postgres + elasticmq + migrate), `--profile full` = adds all six app services. Default profile keeps the dev loop fast (Python on host, hot reload); full profile mirrors W3 production-like behavior.
- **`.env.example` committed**, `.env` gitignored — env-var contract is discoverable.

### D13. Out-of-scope deviations from course spec (Q4, Q5b)

Two intentional deviations from the course material, both defended in `course-spec.md` § 10:

- **Postgres instead of DynamoDB/Cassandra**. The course suggests NoSQL "due to 10K writes/sec." We interpret 10K as peak **scheduling requests** (front-door API), not sustained worker throughput; SQS absorbs the burst. Postgres + `FOR UPDATE SKIP LOCKED` + `PARTITION BY RANGE` + outbox-in-one-transaction outperform DynamoDB for our access patterns at this scale.
- **No internal API Gateway between MCP server and Job Scheduling Service**. The course separates them as distinct services (multi-team architecture). For prototype, they live in the same Python codebase with direct function calls — no internal API Gateway, no extra latency, no extra IaC. External front door remains ALB.

---

## Testing Decisions

### What makes a good test (here)

- **Test external behavior, not implementation**: `task.create@v1` should return `{ok: true, data: {job_id, status: "scheduled"}}` for valid input — don't assert which SQL the handler emitted.
- **Test the seams that matter**: the boundary between MCP envelope and domain logic, the action registry dispatch, the claim-and-mark race, the outbox transactional atomicity.
- **Don't mock the database**. Integration tests run against the real Postgres in Docker Compose. Mocking SQLAlchemy is high-effort low-value because our bugs will be in the SQL semantics (e.g., SKIP LOCKED behavior, transaction isolation).
- **Mock the network** (HTTP calls in `http_call` action, future LLM calls in `llm_*` actions) — these are slow, flaky, and outside the scope of correctness testing.

### Modules with W1 test coverage targets

| Module | Test type | What's covered |
|---|---|---|
| `mcp.handlers` | Unit (no DB) | Argument validation; envelope shape; structured error mapping (USER_INPUT / NOT_FOUND / INVALID_STATE) |
| `mcp.registry` | Unit | Unknown tool → INTERNAL/USER_INPUT error; correct dispatch |
| `domain.jobs` / `domain.runs` | Integration (real Postgres) | Create one-shot job creates 1 JobRun + 1 RunEvent; cancel terminates correctly; claim_run wins exactly one for two competing workers |
| `actions.echo`, `actions.http_call` | Unit (mock httpx) | Echo returns expected; http_call retryable=true for 5xx, false for 4xx, timeout → retryable error |
| `workers.watcher` | Integration | Inserts a future-dated run, advances time, watcher publishes to (mock) SQS; doesn't pick up CANCELLED jobs |
| `workers.executor` | Integration | Pulls a queued run, dispatches echo action, completes; on action failure, marks RETRYING and re-queues; on max retries, executor marks RETRYING (reconciler closes to FAILED — see `workers.reconciler`) |
| `workers.reconciler` | Integration | DLQ-orphaned RETRYING row past grace → flipped to FAILED + RunEvent(dlq_reconcile); stuck QUEUED row past grace → SQS re-enqueued + RunEvent(REENQUEUED); fresh rows inside grace window → untouched |
| End-to-end inspector flow | Integration | The 6-step `PROMPT.md` validation script: create immediate → status becomes completed; create future → cancel → status cancelled; list shows both |

### Coverage target

80%+ overall, enforced by `pytest-cov` in CI (CI itself ships in W4, but the local test command is part of W1 dev loop).

### Prior art

The Python ecosystem's mature reference for this exact pattern:
- **Oban** (Elixir) — outbox pattern over Postgres with FOR UPDATE SKIP LOCKED. Their docs are the gold standard for the SKIP LOCKED claim semantics we're implementing.
- **`graphile-worker`** (Postgres + Node.js) — same SKIP LOCKED claim pattern.
- **`sqlalchemy.ext.asyncio` integration tests** — pattern for `async with session.begin():` transaction scoping.
- The scaffold's `scheduler.py` (now deleted) had a thread-based version of watcher/worker we're now async-ifying.

---

## Out of Scope

These belong to later weeks (W2/W3/W4) or the future-upgrade list in `system-design.md` § 9:

### Out of scope for W1, in scope for W2 (bonuses)

- Cron expression parsing and RecurringJobWatcher actually expanding next instances. (Schema and watcher skeleton are in W1; the cron parsing logic is W2.)
- ChainWatcher actually flipping WAITING → PENDING. (Schema in W1; logic in W2.)
- LLM action handlers (`llm_summarize`, `llm_chat`).
- `send_email` action (mocked or real via SES).
- MCP `resources` (exposing jobs as readable resources).
- MCP `prompts` (templates like `daily_review`).
- Natural-language parsing in the MCP layer that converts user messages into action+params before persisting (the LLM-bonus path).

### Out of scope for W1, in scope for W3 (deployment)

- Terraform modules for ECS Fargate / RDS / ElastiCache / ALB / SQS / IAM / VPC.
- GitHub Actions CI/CD: lint, test, build, push to ECR, ECS deploy.
- AWS Budgets alert + IAM least-privilege roles.
- ALB OIDC integration replacing the `X-User-Id` trust header.
- RDS Proxy or pool downsizing decision.
- Native `PARTITION BY RANGE` upgrade for `job_runs` (with partition automation cron).

### Out of scope for W1, in scope for W4 (polish)

- CloudWatch metrics, dashboards, alarms.
- Structured JSON logging.
- Lambda-based worker variant for the trade-off blog post.
- Demo video, blog post, README polish, architecture diagram in README.

### Permanently out of scope (would change the project)

- DAG-based job dependencies (we chose linear chaining; DAG → Airflow territory).
- Dynamic action plugin loading.
- Multi-region active-active.
- Real OAuth / API key issuance (we delegate to ALB OIDC in W3).
- A Web UI (this is a backend portfolio piece; UI dilutes the signal).

---

## Further Notes

### Companion documents (must-read for implementers)

- **`.doc/session/grilling-state.md`** — decision ledger Q1–Q15, single source of truth for "why each thing is the way it is".
- **`.doc/learn/system-design.md`** — full schema, 4-week plan, 27-item upgrade backlog.
- **`.doc/learn/mcp-protocol.md`** — MCP transport details and tool design rationale.
- **`.doc/learn/course-spec.md`** — course material structured + our deviations defended.
- **`.doc/learn/aws-deep-dive.md`** — quotas/gotchas relevant for W3.
- **`.doc/learn/interview-questions.md`** — accumulated 60+ Q&A keyed by topic; use as study guide before interviews.
- **`PROMPT.md`** — the original course assignment + verification checklist for W1 inspector flow.

### Verification

W1 is "done" when the **MCP Inspector flow in `PROMPT.md` § Verification step 2** passes end-to-end:
1. Connect via inspector → 5 tools (we ship 5 vs course's 4) appear.
2. `task.create@v1` with past `scheduled_at` → returns `job_id`, status `scheduled`.
3. After ~10s, `task.status@v1` → status `completed`.
4. `task.create@v1` with `2099-12-31T00:00:00Z` → returns `job_id`.
5. `task.cancel@v1` → status `cancelled`.
6. `task.list@v1` → both jobs visible.

The inspector flow is not a substitute for unit/integration tests but is the single externally-observable acceptance criterion for W1 prototype completion.

### Risks and mitigations

- **Risk: 10K req/sec NFR is sustained, not peak**. If confirmed by the instructor, Postgres single-writer becomes borderline; mitigation is to add read replicas (won't help writes) or shard. Action item: confirm with instructor (open in `grilling-state.md`).
- **Risk: cron parsing turns out hairy in W2** (timezones, DST, leap seconds). Mitigation: use a battle-tested library (`croniter` for Python) rather than rolling our own.
- **Risk: ElasticMQ behavior diverges from real SQS** (especially around message attributes, FIFO semantics, max receive count). Mitigation: integration tests against a real SQS in W3 staging before W3 production deploy.
- **Risk: AsyncSQLAlchemy lazy-loading bugs in production** (DetachedInstanceError, MissingGreenlet). Mitigation: `expire_on_commit=False` + always use `selectinload` for relationships + integration tests covering serialization paths.

### Decision provenance

Every decision in this PRD is traceable to one of:
- A `Q#` entry in `grilling-state.md` (the grilling session's decision log), or
- A section of `course-spec.md` (course material we adopted), or
- A section of `course-spec.md § 10` (course material we deliberately deviated from, with answer-back).

If a future implementer disagrees with any decision, that's the audit trail to revisit.

---

*Generated 2026-05-12 from .doc/session/grilling-state.md (Q1–Q15) + .doc/learn/* via /to-prd.*
