# CONTEXT — ChatGPT Task Scheduler

Domain glossary for this project. Read this first when a term feels ambiguous; trust this file over the PRD or grilling-state when they disagree.

## §1 Core entities

The system stores three distinct things. Confusing them is the most common source of bugs.

| Entity | Mutable? | One row per | Holds |
|---|---|---|---|
| **`Job`** | Yes | scheduled task definition | what to run, when, who owns it, idempotency key, schedule spec |
| **`JobRun`** | Limited (status transitions only) | one execution attempt | claim state, retry count, result, audit timestamps |
| **`RunEvent`** | No (append-only) | one state transition | event type, occurred_at, `processed_by` JSONB |

A recurring `Job` has many `JobRun`s over time. A one-shot `Job` has exactly one `JobRun`.

## §2 Status lifecycle

The system uses 8 internal statuses but only exposes 5 to MCP clients.

### Internal (DB column `job_runs.status`)

```
PENDING ──▶ QUEUED ──▶ RUNNING ─┬─▶ SUCCEEDED
                                ├─▶ FAILED
                                └─▶ RETRYING ──▶ (back to QUEUED)

(any non-terminal) ──▶ CANCELLED
WAITING ──▶ PENDING    (when blocking-job terminates favourably)
WAITING ──▶ CANCELLED  (when blocking-job terminates unfavourably)
```

### External (returned by `task.status.v1` / `task.list.v1`)

| Internal | External |
|---|---|
| PENDING, QUEUED, WAITING | `scheduled` |
| RUNNING, RETRYING | `running` |
| SUCCEEDED | `completed` |
| FAILED | `failed` |
| CANCELLED | `cancelled` |

Mapping happens at the MCP handler boundary. DB keeps the precise truth; LLM gets the simple model.

## §3 Schedule types

`Job.schedule_type` is one of:

- **`immediate`** — runs as soon as a worker picks it up. `scheduled_at = now()` is set at create time.
- **`one-shot`** — runs at a specific future `scheduled_at` (ISO 8601 datetime). `timezone` defaults to UTC.
- **`recurring`** — uses `cron_expr` (W1: schema only; W2: cron expansion in `RecurringJobWatcher`). Each occurrence spawns a fresh `JobRun` after the previous one terminates.

`scheduled_at` and `cron_expr` are mutually exclusive per `Job`.

## §4 Execution model

### Action vs ActionHandler vs ActionResult

- **`Action`** — a string identifier like `"echo"` or `"http_call"`, stored in `Job.action`. Whitelisted by the action registry.
- **`ActionHandler`** — the Python class implementing `execute(run, params) -> ActionResult`. Declares its own Pydantic `params_model` and `timeout_seconds`.
- **`ActionResult`** — what the handler returns: `{ok, result, error, retryable}`. `retryable=False` ⇒ permanent failure ⇒ skip retry ⇒ DLQ.

### Process roles

| Process | Job | How it scales |
|---|---|---|
| **`mcp-server`** | Handles MCP tool calls (stdio or HTTP) | Stateless; scale horizontally |
| **`Watcher`** | Scans for due `JobRun`s within the `lookahead window` (5 min), publishes to queue | Multiple instances safe via `FOR UPDATE SKIP LOCKED` (no leader election) |
| **`Worker`** | Pulls from queue, claims via DB UPDATE, dispatches to `ActionHandler` | Multiple instances safe via `claim-and-mark` |
| **`RecurringJobWatcher`** | Reads `run_events` for terminal events of recurring jobs; inserts next `JobRun` | Single instance for W1 |
| **`ChainWatcher`** | Reads `run_events` for terminal events; flips `WAITING` runs to `PENDING` (or `CANCELLED`) based on `trigger_on_status` | Single instance for W1 |
| **`migrate`** | One-shot; runs Alembic migrations before app services start | Compose `service_completed_successfully` |

### Key primitives

- **`claim-and-mark`** — `UPDATE job_runs SET status='RUNNING' WHERE run_id=:rid AND status IN ('PENDING','QUEUED') RETURNING ...`. Atomic. Only one worker wins on duplicate delivery.
- **`lookahead window`** — the 5-minute future horizon the Watcher considers due. Matches SQS `DelaySeconds` max for our use case.
- **`heartbeat`** — every 30 seconds while a long action runs, the Worker calls `ChangeMessageVisibility` to extend SQS visibility. On crash, the timeout expires and the message becomes visible to another worker.

### Chain-fed handlers and the inter-handler data plane

- **`chain-fed handler`** — an `ActionHandler` whose `params_model` includes the optional field `from_run_id: int | None`. When `from_run_id` is non-null at execution time, the handler reads the upstream `JobRun.result` as its primary input instead of (or in addition to) its own params. See ADR-033.
- **`inter-handler data plane`** — the column `JobRun.result` (a JSON string) is the data carrier between chained handlers. The upstream handler serializes its output into `ActionResult.result`; the downstream handler reads it via `app.chain.upstream_reader.read_upstream`. `ChainWatcher` handles status coordination (WAITING → PENDING) but never touches `result` — those are separate concerns.

## §5 Data patterns

- **`outbox`** (transactional outbox pattern) — every status transition writes a `RunEvent` in the **same transaction** as the `job_runs.status` update. Downstream consumers (`RecurringJobWatcher`, `ChainWatcher`) read the immutable event log, never the mutable status column. Eliminates a class of races.
- **`time_bucket`** — partition key column on `job_runs` (e.g. `date_trunc('hour', scheduled_at)`). W1: column + composite PK + partial index. W2: upgraded to native `PARTITION BY RANGE`. Lets the Watcher's "what's due in the next 5 min" query stay O(log n) within the current hour.
- **`idempotency_key`** — `Job.idempotency_key` is `UNIQUE`. Same key + same `user_id` returns the existing `Job` instead of creating a duplicate. Caller-side retry safety.
- **`FOR UPDATE SKIP LOCKED`** — Postgres locking clause used by the Watcher's claim query. Skips rows another transaction is locking; multiple watchers never contend on the same row.

## §6 MCP surface

### Tool vs Action — the most-confused pair

| | **Tool** | **Action** |
|---|---|---|
| Whose vocabulary | MCP client (LLM) | Internal worker dispatch |
| Examples | `task.create.v1`, `task.list.v1` | `echo`, `http_call`, `llm_summarize` (W2) |
| How many | 5 in W1 | 2 in W1, grows in W2 |
| Where defined | `app/mcp/tools/` | `app/actions/` |

A `Tool` is *what an LLM client can invoke*. An `Action` is *what the worker can execute*. `task.create.v1` is the Tool; it accepts an `action` field whose value is one of the registered Action names.

### Envelope

Every tool response wraps its payload:

```
Success: {"ok": true, "data": {...}}
Failure: {"ok": false, "error": {"code", "message", "field", "expected"}}
```

`code` is drawn from a fixed 6-word vocabulary:

| Code | Meaning |
|---|---|
| `USER_INPUT` | Caller's args failed schema validation or a business rule |
| `NOT_FOUND` | `job_id` doesn't exist or isn't owned by this user |
| `INVALID_STATE` | Tried to cancel a terminal job, or a similar state-violation |
| `UNKNOWN_ACTION` | `Job.action` not in registry |
| `DUPLICATE` | `idempotency_key` collision |
| `INTERNAL` | Server-side failure not caused by the caller |

### Versioning (`.v1`)

Every tool name carries `.v1`. MCP clients cache `tools/list` per thread; changing a tool's schema mid-thread breaks long conversations. To evolve, ship `task.foo.v2` alongside `.v1`.

### Transport

The MCP server module is transport-agnostic. Two entrypoints select transport:

- **stdio** — for Claude Desktop, MCP Inspector, subprocess clients. Reads `MCP_USER_ID` env var.
- **Streamable HTTP** — listens on `$PORT`. Reads optional `X-User-Id` header (falling back to env var).

### user_id resolver

A single function determines `user_id`:

1. `X-User-Id` HTTP header (HTTP transport only)
2. `MCP_USER_ID` env var
3. literal `"default-user"`

Trust-only in W1–W4 (local / single-operator only — never safe to expose publicly).

**W5 public pivot (ADR-049):** the public deployment becomes multi-tenant via OAuth 2.1
delegation. `user_id` is the verified token subject (`sub`), not a self-asserted header.
The trust-only header path survives only for local stdio (Claude Desktop) and the
operator's own access.

### Credential model (dual-track, ADR-050)

Two parallel, non-overlapping ways an action obtains a downstream credential:

| Caller | Mechanism | Storage |
|---|---|---|
| Public (delegated) user | per-user OAuth connection (GitHub / Slack / Google) | encrypted token — scoped, revocable, auto-refreshed |
| Operator (you) | `${VAR}` env substitution (ADR-032, unchanged) | VPS `.env`, operator's own keys only |

The system never stores a public user's raw long-lived secret. `${VAR}`-from-env actions
(SMTP, R2, arbitrary-key `http_call`) are **operator-only** (`requires_operator`,
ADR-051) — rejected at `task.create` for delegated users.

**Exception — operator-funded public LLM actions (ADR-052):** `llm_summarize` /
`llm_polish` are *public-invokable but operator-funded*. They use the operator's LLM key
internally for a fixed, cost-capped transform; users cannot reference `${VAR}` and cannot
supply a free-form prompt. This is the only case where a public action touches an operator
env secret.

## §7 Chaining & recurring

### Chaining (linear only, no DAG)

Set `Job.trigger_on_job_id` to make this `Job`'s runs wait for another `Job` to terminate. `Job.trigger_on_status` is one of:

- `SUCCEEDED` — flip to `PENDING` only if blocker succeeded; else `CANCELLED`
- `FAILED` — flip to `PENDING` only if blocker failed; else `CANCELLED`
- `ANY` — flip to `PENDING` on any terminal status

The downstream `Job`'s initial `JobRun` is created with `status='WAITING'` and `wait_for_run_id` set. `ChainWatcher` flips it when the blocker terminates.

**Data flow in chained handlers** is a separate convention layered above `ChainWatcher`. See `chain-fed handler` and `inter-handler data plane` in §4, and ADR-033 for the full specification including the recommended Design B pattern (`trigger_on_status=ANY` + downstream internal ok-path/error-path branching) and the anti-pattern (do NOT create handlers like `slack_post_from_github_digest` — specialisation is via params, not class names).

W1: schema in place. W2: `ChainWatcher` logic. W4: `from_run_id` convention + `upstream_reader` module.

### Recurring

`Job.schedule_type='recurring'` + `Job.cron_expr` + `Job.timezone`. The system creates exactly one `JobRun` at a time. On terminal `RunEvent`, `RecurringJobWatcher` parses the cron expression to find the next occurrence and inserts the next `JobRun`.

W1: schema + watcher skeleton. W2: cron expansion (using `croniter`).

## §8 Operational vocab

- **`DLQ` (dead-letter queue)** — SQS-side feature. When a message hits `max_receive_count` (3) without being deleted, SQS routes it to the configured DLQ. App code does nothing; configuration is in IaC (W3) or `aws sqs` calls (W1).
- **`visibility timeout`** — SQS-side. After a worker calls `ReceiveMessage`, the message is hidden for N seconds (initial 60). The Worker extends this via `heartbeat`. On crash, the message becomes visible again.
- **`max receive count`** — set to 3. After 3 failed deliveries, DLQ.
- **`connection pool sizing per role`** — each process role owns its own pool. W1 doesn't care; W3 must reconcile total (130) vs RDS `max_connections` (81 on db.t4g.micro). Options: tune pools down, upgrade RDS tier, or add RDS Proxy.
- **`pool_pre_ping`** — SQLAlchemy setting that issues a cheap query on each checkout to detect stale connections. Always on.
- **`pool_recycle=3600`** — drop connections older than an hour. Survives idle-disconnect from RDS or any TCP middlebox.
