# CONTEXT — Owl Task Scheduler MCP

Domain glossary for this project. Read this first when a term feels ambiguous; trust this file over the PRD or grilling-state when they disagree.

## §0 Project naming

The project carries **three distinct names on purpose** — they are deliberately decoupled, and confusing them (or "fixing" the divergence) is a trap. See ADR-066 (relates ADR-044, ADR-061).

| Name | Value | Audience / where it lives |
|---|---|---|
| **display name** (brand) | `Owl Task Scheduler MCP` | Humans — README, landing page, OG metadata, docs prose |
| **routing identifier** | `owl-scheduler` | The LLM — MCP server self-reported name (`Server("owl-scheduler", …)`) and the suggested `claude_desktop_config.json` key |
| **infra identifier** | `task_scheduler_mcp` | Machines — GitHub repo, Python import root is `app`, container image, VPS `/opt/task_scheduler_mcp`, Terraform tags, R2 buckets |

Why three, not one:

- The **routing identifier** leads with the distinctive token `owl` because the bare term *"task scheduler"* collides with Claude's first-party scheduler — the LLM was routing to the built-in and denying our features. `owl-scheduler` is the collision-free handle a user can say ("use owl-scheduler to…") to force routing here. Disambiguation lives here + in the server `instructions` (capability-based routing directive, ADR-061), **not** in the human brand or the repo name (the LLM never reads those).
- The **infra identifier** stays `task_scheduler_mcp` from ADR-044. It is invisible to end users and re-renaming it again (image paths, `/opt`, CI, Terraform) is pure churn with zero effect on the routing collision.
- The **tool namespace** `task.*.v1` and resource scheme `tasks://` are **unchanged** (they are a versioned API contract, ADR-014); the `owl-scheduler` server namespace already disambiguates them (`owl-scheduler › task.create.v1`).

## §1 Core entities

The system stores three distinct things. Confusing them is the most common source of bugs.

| Entity | Mutable? | One row per | Holds |
|---|---|---|---|
| **`Job`** | Yes | scheduled task definition | what to run, when, who owns it, idempotency key, schedule spec |
| **`JobRun`** | Limited (status transitions only) | one execution attempt | claim state, retry count, result, audit timestamps |
| **`RunEvent`** | No (append-only) | one state transition | event type, occurred_at, `processed_by` JSONB |

A recurring `Job` has many `JobRun`s over time. A one-shot `Job` has exactly one `JobRun`.

**`Job.state`** — the Job's lifecycle, an explicit enum `active | completed | cancelled` (ADR-068), kept **separate** from the run-level `job_runs.status` (§2). It answers *"can this schedule still do anything?"*, never *"did an execution succeed?"* — that stays on `JobRun.status`. Replaces a never-cleared `active` boolean that made every job ever created count against the quota forever (the quota-lockout bug).

- **`active`** — the Job still carries **resident load**: it can still produce or run a future `JobRun`. *Active* is **not** "ever created and not cancelled" — it is the predicate the containment caps (ADR-055) bound, because resident load, not lifetime job count, is what consumes the box.
- **`completed`** — the schedule is **exhausted**; no more runs. This is **not** "succeeded": an `immediate`/`one-shot` job settles to `completed` the moment its single `JobRun` reaches *any* terminal status (success/failure lives on `JobRun.status`). A `recurring` root is **never** auto-completed — it stays `active` until cancelled.
- **`cancelled`** — a user explicitly stopped it (`task.cancel`); no more future runs, and any in-flight `JobRun` is left to finish (ADR-022). `cancelled_at` is retained as an audit timestamp only.

The containment quota counts `state = 'active'`; `completed`/`cancelled` jobs leave the active set. The one-shot `→ completed` settle and a concurrent `→ cancelled` are both compare-and-set from `active` (first writer wins), so a one-shot whose run finishes while a cancel lands stays `cancelled` (user intent) with `JobRun.status = succeeded` (what happened). A `trigger-driven` (chained) job settles to `completed` when its trigger parent is terminal (`completed`/`cancelled`) **and** it has no non-terminal run — one-hop parent propagation driven by the continuation consumer (§7, ADR-068 §2); cancelling a job cascades this settle to its downstreams, so a cancelled upstream stops its not-yet-run downstreams and frees their quota.

## §2 Status lifecycle

The system uses 7 internal statuses but only exposes 5 to MCP clients.

### Internal (DB column `job_runs.status`)

```
PENDING ──▶ QUEUED ──▶ RUNNING ─┬─▶ SUCCEEDED
                                ├─▶ FAILED
                                └─▶ RETRYING ──▶ (back to QUEUED)

(any non-terminal) ──▶ CANCELLED
```

### External (returned by `task.status.v1` / `task.list.v1`)

| Internal | External |
|---|---|
| PENDING, QUEUED | `scheduled` |
| RUNNING, RETRYING | `running` |
| SUCCEEDED | `completed` |
| FAILED | `failed` |
| CANCELLED | `cancelled` |

Mapping happens at the MCP handler boundary. DB keeps the precise truth; LLM gets the simple model.

> **`WAITING` was removed (ADR-067).** The pre-arm control plane — a downstream run pre-created `WAITING` and flipped by `ChainWatcher` — is gone. Under continuation run-creation (§7) a chained downstream has zero runs until its upstream terminates, then a `PENDING` run is created directly. A not-yet-triggered downstream shows externally as `scheduled` with an empty `runs` list (§7), not as a `WAITING` run.

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
| **`continuation consumer`** (the generalised `RecurringJobWatcher`) | Reads `run_events` for terminal events in `event_id` order; per event materializes the recurring successor **and** each matching trigger-driven downstream run via `RunMaterializer`, then drives the trigger-driven `Job.state` settle + cascade (ADR-067 / ADR-068 §2) | Single instance for W1 |
| **`migrate`** | One-shot; runs Alembic migrations before app services start | Compose `service_completed_successfully` |

### Key primitives

- **`claim-and-mark`** — `UPDATE job_runs SET status='RUNNING' WHERE run_id=:rid AND status IN ('PENDING','QUEUED') RETURNING ...`. Atomic. Only one worker wins on duplicate delivery.
- **`lookahead window`** — the 5-minute future horizon the Watcher considers due. Matches SQS `DelaySeconds` max for our use case.
- **`heartbeat`** — every 30 seconds while a long action runs, the Worker calls `ChangeMessageVisibility` to extend SQS visibility. On crash, the timeout expires and the message becomes visible to another worker.

### Chain-fed handlers and the inter-handler data plane

- **`chain-fed handler`** — an `ActionHandler` whose `params_model` includes the optional field `from_run_id: int | None`. When `from_run_id` is non-null at execution time, the handler reads the upstream `JobRun.result` as its primary input instead of (or in addition to) its own params. See ADR-033.
- **`inter-handler data plane`** — the column `JobRun.result` (a JSON string) is the data carrier between chained handlers. The upstream handler serializes its output into `ActionResult.result`; the downstream handler reads it via `app.chain.upstream_reader.read_upstream`. The continuation consumer creates the downstream run and stamps `wait_for_run_id` = the upstream terminal run_id; the executor injects `from_run_id = wait_for_run_id` so the handler knows which upstream result to read (ADR-064). Run *creation* and *result* are separate concerns — creation never touches `result`.
  - **`result` is internal-only.** No MCP surface exposes it to the client: `task.status.v1`, `tasks://job/{id}`, and `tasks://recent-results` (ADR-037) are all *metadata* (status, timestamps, error excerpt). `result` exists solely to feed a downstream handler. Corollary: the operator-funded LLM actions (`llm_polish` / `llm_summarize`, ADR-052) deliver value **only as chain upstreams** — a standalone run executes and stores its output, but the user cannot read it back. Documentation and demo prompts must present them as chain steps (e.g. `llm_polish → slack_post`), never as a standalone "rewrite this and show me."

## §5 Data patterns

- **`outbox`** (transactional outbox pattern) — every status transition writes a `RunEvent` in the **same transaction** as the `job_runs.status` update. The downstream `continuation consumer` reads the immutable event log, never the mutable status column. Eliminates a class of races.
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

All run creation routes through one stateless domain module (not a process), the **`RunMaterializer`**. It owns *what runs should exist and in what initial state*:

- `create_job` → `materialize_initial` (a schedule-driven job's first `PENDING` run; a trigger-driven job gets **no** initial run)
- the continuation consumer → `materialize_successor` (the next cron occurrence) **and** `materialize_downstream` (a trigger-driven downstream run)

**`continuation`** — a trigger-driven downstream run is **created when its upstream run reaches a terminal status** (ADR-067), by the single continuation consumer, in the same transaction that materializes the recurring successor. There is no pre-armed `WAITING` run and no recursive `arm`: one rule — *terminal event → materialise the next run* — governs recurrence **and** chaining. The new downstream run is `PENDING` with `wait_for_run_id` set to the upstream terminal `run_id` (the **data plane**: the executor injects `from_run_id = wait_for_run_id`, ADR-064). Creation is idempotent at the data layer — partial unique indexes on the run's cause, `(job_id, wait_for_run_id)` for trigger-driven and `(job_id, scheduled_at)` for schedule-driven successors — so a redelivered terminal event is a no-op; the `processed_by` cursor is only an efficiency layer. Terminal events are processed in `event_id` order.

> The legacy `WAITING` status, `ChainWatcher` (status flip), and pre-armed `arm`/`re-arm` model (ADR-065) are **superseded by ADR-067 and removed**: `ChainWatcher` (process, worker, entrypoint, tests) is deleted, `WAITING` is dropped from the status machine (§2, migration 0012), and a chained downstream's `Job.state` settle is driven by the continuation consumer (ADR-068 §2).

### `trigger_on_status` — the create predicate

Applied at **create** time (ADR-067), not flip time:

- `SUCCEEDED` — create the downstream run only if the upstream **succeeded**
- `FAILED` — create only if the upstream **failed**
- `ANY` — create on `SUCCEEDED` **or** `FAILED`

A **user-cancelled** upstream is the exception to `ANY`: it does **not** fire downstreams. Cancelling a job stops its whole pipeline — the cancel cascade settles the not-yet-run downstreams to `completed` (`Job.state`, ADR-068 §2) and the continuation consumer never materializes a downstream off a `CANCELLED` run. (Under continuation a `CANCELLED` run only ever comes from a job cancel, so there is nothing left for "ANY includes CANCELLED" to create.)

A **predicate miss** creates no run and records a lightweight audit `RunEvent` (`CHAIN_SKIPPED_PREDICATE_MISS`) — there is no `CANCELLED_BY_CHAIN_MISS` run. Recommended **Design B**: `trigger_on_status=ANY` + downstream internal ok/error branching, so the sink always notifies (success or fallback). Do **not** create source-coupled handler classes like `slack_post_from_github_digest` — specialisation is via params, not class names (ADR-033).

### `fan-out` vs `fan-in` (the real meaning of "no DAG")

- **`fan-out`** — one upstream → many downstream (e.g. `llm_summarize → slack_post` **+** `email_send`). **Allowed.** On the shared upstream terminal event, one run is created per matching downstream job, independently.
- **`fan-in`** — one downstream reading *many* upstreams (`from_run_ids: list[int]`). **Deferred** (ADR-033 future / ADR-040). This — not fan-out — is what "linear only, no DAG" excludes.

### `slow consumer`

If an upstream produces terminal events faster than a downstream finishes, the overlapping downstream tick is **not created** (load-shedding), preserving the invariant **at most one *executing* `JobRun` per `Job`** — where *executing* means `PENDING` / `QUEUED` / `RUNNING` / `RETRYING`. When the upstream terminates, the continuation consumer checks the shared `has_executing_run` predicate for the downstream job: if it already has an executing run, the create is **skipped** with an audited `CHAIN_SKIPPED_SLOW_CONSUMER` event — nothing is created then cancelled. The dropped tick's data is simply not consumed; the downstream catches the next idle tick.

History: W1 schema; W2 `ChainWatcher` + cron expansion (`croniter`); W4 `from_run_id` convention + `upstream_reader`; the `RunMaterializer` + `inherited recurrence` model is ADR-065; **continuation run-creation (this section) is ADR-067** — recurrence and chaining unified under one terminal-event rule, exactly-once moved to a data-layer unique constraint, replacing the pre-armed `WAITING`/`ChainWatcher` control plane and its arm-race class (#227 / #234-237).

## §8 Operational vocab

- **`DLQ` (dead-letter queue)** — SQS-side feature. When a message hits `max_receive_count` (3) without being deleted, SQS routes it to the configured DLQ. App code does nothing; configuration is in IaC (W3) or `aws sqs` calls (W1).
- **`visibility timeout`** — SQS-side. After a worker calls `ReceiveMessage`, the message is hidden for N seconds (initial 60). The Worker extends this via `heartbeat`. On crash, the message becomes visible again.
- **`max receive count`** — set to 3. After 3 failed deliveries, DLQ.
- **`connection pool sizing per role`** — each process role owns its own pool. W1 doesn't care; W3 must reconcile total (130) vs RDS `max_connections` (81 on db.t4g.micro). Options: tune pools down, upgrade RDS tier, or add RDS Proxy.
- **`pool_pre_ping`** — SQLAlchemy setting that issues a cheap query on each checkout to detect stale connections. Always on.
- **`pool_recycle=3600`** — drop connections older than an hour. Survives idle-disconnect from RDS or any TCP middlebox.
