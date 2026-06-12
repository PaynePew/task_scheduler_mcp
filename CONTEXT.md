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

`code` is drawn from a fixed 7-word vocabulary (ADR-014, amended ADR-060):

| Code | Meaning |
|---|---|
| `USER_INPUT` | Caller's args failed schema validation or a business rule |
| `NOT_FOUND` | `job_id` doesn't exist or isn't owned by this user |
| `INVALID_STATE` | Tried to cancel a terminal job, rate/concurrency limit, overload, or a similar state-violation |
| `UNKNOWN_ACTION` | `Job.action` not in registry |
| `DUPLICATE` | `idempotency_key` collision |
| `INTERNAL` | Server-side failure not caused by the caller |
| `MISSING_CONNECTION` | Required OAuth connection not set up; carries optional `connect_url` field |

### Versioning (`.v1`)

Every tool name carries `.v1`. MCP clients cache `tools/list` per thread; changing a tool's schema mid-thread breaks long conversations. To evolve, ship `task.foo.v2` alongside `.v1`.

### Transport

The MCP server module is transport-agnostic. Two entrypoints select transport:

- **stdio** — for Claude Desktop, MCP Inspector, subprocess clients. Reads `MCP_USER_ID` env var.
- **Streamable HTTP** — listens on `$PORT`. Reads optional `X-User-Id` header (falling back to env var).

### Bearer verify posture

How this process verifies an incoming bearer token (HTTP transport only).
Defined as a sum type in `app/auth/posture.py`; derived from `Settings` at
startup via `bearer_posture_from_settings()` and consumed by the `mcp-server`
HTTP entrypoint:

- **`TrustOnly`** — no bearer verification. Falls back to the `X-User-Id`
  header per the `user_id resolver`. Legal for W1–W4 (local / single-operator)
  and for stdio transport at all times.
- **`BearerVerified(jwks_uri, issuer, audience)`** — verifies the bearer's
  signature against the JWKS, asserts `iss` + `aud`, returns the verified
  subject (`sub`). The verify *mechanism* (JWKS fetch, key cache, PyJWT call)
  lives in `validate_token()`; the posture is a record describing how to
  call it. Vendor-agnostic name on purpose — WorkOS is the current IdP, but
  any JWKS-verifying issuer slots into the same variant.

Posture is *derived* from raw Settings, never inline. Partial config (some
but not all of jwks_uri/issuer/audience) raises at startup — there is no
third silent-trust-only state. This closes the silent-downgrade hole that
required PR #169 + #171 to fix in two passes.

W5 (ADR-049) adds `OAuthDelegated(...)` as a third variant; because the type
is a sum, every `match posture` site is forced to handle the new mode at
type-check time.

Independent of `OAuthClientPosture` (the server's own OAuth client identity
for outbound code-for-token flows in `app/web/connections.py`). Bearer verify
answers "how do I trust callers"; OAuth client answers "how do I authenticate
*to* WorkOS". Two posture types, two seams.

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

### `run source` — the single cause of a `JobRun`

Every `JobRun` is materialized by exactly one **`run source`**. There are two kinds, and they are **mutually exclusive per `Job`**:

- **`schedule-driven`** — runs come from a clock: `immediate`, `one-shot` (`scheduled_at`), or `recurring` (`cron_expr`). This is the *root* of a chain.
- **`trigger-driven`** — runs come from another `Job`: one downstream run per upstream run, via `trigger_on_job_id`. A trigger-driven `Job` carries **no `cron_expr`** — see `inherited recurrence`.

**`inherited recurrence`** — a chained (trigger-driven) `Job` recurs because *its trigger recurs*, never because it declares its own cron. Corollary: `trigger_on_job_id` and `cron_expr` are mutually exclusive on one `Job` (rejected at `task.create` with `USER_INPUT` — validation rule V6). Declaring both is the footgun that silently double-spawns a downstream.

### `RunMaterializer` — the single owner of run creation

All run creation routes through one stateless domain module (not a process), the **`RunMaterializer`**. It owns *what runs should exist and in what initial state*, and **arming the downstream is an internal, atomic step of materialization** — you cannot create a run without arming its downstream in the same transaction. This is what makes chain coordination structural rather than convention-based.

- `create_job` → `materialize_initial` (first run: a scheduled run, or a `WAITING` run armed against the trigger's current run)
- `RecurringJobWatcher` → `materialize_successor` (the next cron occurrence)
- both, internally → `arm` (recursively)

**`arm` / `re-arm`** — materializing a fresh `WAITING` downstream `JobRun` whose `wait_for_run_id` points at a specific upstream run. Done **once per upstream run, unconditionally** — even while the previous tick's downstream run is still in flight — cascading down the chain (and across `fan-out`). This is the **control plane**. `ChainWatcher` then flips `WAITING → PENDING/CANCELLED` (status only); the executor injects `from_run_id = wait_for_run_id` (the **data plane**, ADR-064). Three separate concerns: materialize *creates*, ChainWatcher *coordinates status*, executor *moves data* — never merged (ADR-033). Load-shedding is **not** an arm-time concern; the overlap is resolved later at flip time (see `slow consumer`).

### `trigger_on_status` — the flip predicate

- `SUCCEEDED` — flip to `PENDING` only if blocker succeeded; else `CANCELLED`
- `FAILED` — flip to `PENDING` only if blocker failed; else `CANCELLED`
- `ANY` — flip to `PENDING` on any terminal status (including `CANCELLED`)

Recommended **Design B**: `trigger_on_status=ANY` + downstream internal ok/error branching, so the sink always notifies (success or fallback). Do **not** create source-coupled handler classes like `slack_post_from_github_digest` — specialisation is via params, not class names (ADR-033).

### `fan-out` vs `fan-in` (the real meaning of "no DAG")

- **`fan-out`** — one upstream → many downstream (e.g. `llm_summarize → slack_post` **+** `email_send`). **Allowed.** Each downstream is its own linear chain off the shared upstream run, with its own `WAITING` run pointing at it.
- **`fan-in`** — one downstream reading *many* upstreams (`from_run_ids: list[int]`). **Deferred** (ADR-033 future / ADR-040). This — not fan-out — is what "linear only, no DAG" excludes.

### `slow consumer`

If an upstream produces runs faster than a downstream finishes, the overlapping downstream tick is **dropped** (load-shedding), preserving the invariant **at most one *executing* `JobRun` per `Job`** — where *executing* means `PENDING` / `QUEUED` / `RUNNING` / `RETRYING`. A `WAITING` run armed for the next tick is **not** executing, so it may coexist with the current tick's executing (or not-yet-flipped `WAITING`) run; the shared `has_executing_run` predicate excludes `WAITING`. The drop happens at **flip time** (`ChainWatcher`: `WAITING → CANCELLED` with an audited `CANCELLED_SLOW_CONSUMER` event), which is the single load-shedding path — arming is unconditional. The dropped tick's data is simply not consumed; the downstream catches the next idle tick.

> **Supersedes #227** (operator decision, 2026-06-12): the earlier wording was "at most one *live* (non-terminal) run", with a spawn-time forbid-concurrency check applied to **all** spawns. That let `re-arm` of tick N+1 silently fail with `ConcurrencyError` whenever tick N's downstream run was still live (the common case under concurrent watchers), so a chained downstream missed every other tick with no audit record. Counting only *executing* runs and routing all load-shedding through the flip-time drop fixes the race. A transient overlap of two `WAITING` runs (current tick not yet flipped + next tick just armed) is now expected, not a violation. See ADR-065 §4.

History: W1 schema; W2 `ChainWatcher` + cron expansion (`croniter`); W4 `from_run_id` convention + `upstream_reader`; the `RunMaterializer` + `inherited recurrence` model is ADR-065 (the realisation of the "subscription" run source ADR-020 deferred and ADR-064 mistakenly assumed already existed).

## §8 Operational vocab

- **`DLQ` (dead-letter queue)** — SQS-side feature. When a message hits `max_receive_count` (3) without being deleted, SQS routes it to the configured DLQ. App code does nothing; configuration is in IaC (W3) or `aws sqs` calls (W1).
- **`visibility timeout`** — SQS-side. After a worker calls `ReceiveMessage`, the message is hidden for N seconds (initial 60). The Worker extends this via `heartbeat`. On crash, the message becomes visible again.
- **`max receive count`** — set to 3. After 3 failed deliveries, DLQ.
- **`connection pool sizing per role`** — each process role owns its own pool. W1 doesn't care; W3 must reconcile total (130) vs RDS `max_connections` (81 on db.t4g.micro). Options: tune pools down, upgrade RDS tier, or add RDS Proxy.
- **`pool_pre_ping`** — SQLAlchemy setting that issues a cheap query on each checkout to detect stale connections. Always on.
- **`pool_recycle=3600`** — drop connections older than an hour. Survives idle-disconnect from RDS or any TCP middlebox.
