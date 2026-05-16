# PRD: ChatGPT Task Scheduler — W2 Bonus Implementation

> **Scope**: Bonus implementation on top of the W1 prototype. Recurring (cron) + Job chaining + MCP resources + MCP prompts. Schema migration to align with W2 cancel semantics. **Not** in scope here: AWS deployment (W3), observability/CI/CD/demo video (W4), server-side LLM action — see "Out of Scope" + companion docs.
>
> **Deliverable**: A bonus-completed local MCP-based job scheduler validated by an extended inspector flow plus a Claude Desktop sanity check.
>
> **Status**: Design 100% locked. Source decisions in `.doc/session/grilling-state.md` (Q-W2-1 to Q-W2-16) + this PRD.
>
> **Generated**: 2026-05-16 via `/to-prd` after Grilling Session #3.
>
> **Supersedes**: W1 PRD's "Out of scope for W1, in scope for W2" section. The β path established in this PRD removes server-side LLM (`llm_summarize`) from W2 scope (see D5).

---

## Problem Statement

The W1 prototype proves the scheduling primitive end-to-end with two scheduled actions (`echo`, `http_call`) and immediate / one-shot execution. End users (LLM clients like Claude Desktop, ChatGPT custom connectors) hitting that prototype cannot yet:

1. Schedule **recurring** jobs and have the system spawn the next occurrence automatically — the schema is in place but the expansion logic is not.
2. Compose **chains** of jobs where one job's terminal status releases the next — schema is in place but the `ChainWatcher` is a skeleton.
3. Cancel a recurring job and have the system actually stop future occurrences — W1's `task.cancel.v1` flips a JobRun's status but has no `Job`-level "stop spawning" signal.
4. **Discover** the user's task inventory or the action registry as **passive context** when entering a conversation — MCP `resources` are unimplemented.
5. **Invoke** server-published conversational **starting points** (slash commands) — MCP `prompts` are unimplemented.

Operators and contributors also lack:

6. A **bonus completion gate** — what does "W2 done" mean concretely? W1 had a 6-step inspector flow; W2 needs an equivalent.

The course-spec article also lists a fifth bonus: "Connect a real LLM to parse natural language task descriptions before calling `task.create`". W1 grilling deferred this to W2 as a server-side OpenAI-mediated NL parser. After deeper analysis in Session #3, we re-interpret this bonus: **the LLM in Claude Desktop / Claude Code / Codex IS the real LLM**, and the entire MCP design (strict schema, system instruction, structured fixable errors) IS the NL parser surface. Server-side NL re-parsing would duplicate the client LLM's work for no benefit. This bonus is therefore satisfied by **connecting the MCP to a real LLM-powered client** rather than by shipping a server-side LLM. See D5 / D11 for the full rationale.

---

## Solution

Implement five bonus capabilities on top of W1, plus one schema cleanup, plus one acceptance gate:

1. **`RecurringJobWatcher` cron expansion** — when a recurring `Job` has a terminal `RunEvent`, parse its `cron_expr` with the user's `timezone`, compute the next occurrence (inclusive of `now`), and insert exactly one next `JobRun`. Spawn is gated on `Job.cancelled_at IS NULL`.

2. **`ChainWatcher` linear chain release** — when any `RunEvent` reaches a terminal status, find `WAITING` runs whose `wait_for_run_id` matches the just-terminated run; flip to `PENDING` if `Job.trigger_on_status` matches the terminal status (with `ANY` matching all terminal statuses including `CANCELLED`), else flip to `CANCELLED`.

3. **W2 cancel semantics** — `task.cancel.v1` becomes a **job-level** operation: any `PENDING` / `QUEUED` / `WAITING` / `RETRYING` runs of the job flip to `CANCELLED`; any **currently `RUNNING`** run is left alone to complete naturally; `Job.cancelled_at = now()` is recorded; subsequent recurring expansion is blocked. Behavior is uniform across `schedule_type`. Tool name stays `.v1` (no cached clients yet).

4. **MCP resources** — expose three URIs (`tasks://list`, `tasks://job/{job_id}` template, `tasks://actions`) so MCP clients that support resources receive passive context at conversation start.

5. **MCP prompts** — publish two slash commands (`daily_review`, `setup_summary`) producing light templated user messages that reference the resources.

6. **Schema migration `w2_schema`** — `ALTER TABLE jobs ADD cancelled_at TIMESTAMPTZ NULL; DROP COLUMN raw_user_input; DROP COLUMN parsing_metadata;`. One forward + one reverse, CI-tested.

7. **Acceptance gate** — three layers (CI E2E test, manual MCP Inspector flow, Claude Desktop sanity check); the recorded demo video lives in W4.

Locally: `docker compose up --profile full` brings the full stack online; W2 features exercise the same six entrypoints from W1. The compose footprint does not change.

---

## User Stories

### Recurring schedules (W2)

1. As an LLM client, I want a recurring `Job`'s next `JobRun` to be created automatically after the previous run terminates, so that I do not need to re-schedule each occurrence.
2. As an LLM client, I want the next `scheduled_at` computed in the `Job.timezone` so that "9am" is the user's locale wall-clock rather than UTC.
3. As an LLM client, I want first-run timing to be **inclusive of `now`** so that creating an `@daily` job at 08:00:00.000 fires at today 08:00:00.000 rather than tomorrow.
4. As an LLM client, I want a daylight-savings transition not to break or duplicate occurrences — spring-forward "skipped" hours advance to the next valid time; fall-back "duplicate" hours fire only once.
5. As an LLM client, I want at most one in-flight `JobRun` per recurring `Job` (`Forbid` concurrency policy) so that long-running occurrences don't pile up parallel duplicates.
6. As an LLM client, I want a malformed `cron_expr` or unknown `timezone` to be rejected at `task.create.v1` with a `USER_INPUT` error citing the offending field and expected format.
7. As an LLM client, I want `@daily`, `@hourly`, `@weekly`, `@monthly`, `@yearly` shortcuts to work, so that I do not need to translate common cadences into 5-field cron expressions.

### Timezone resolution (W2)

8. As an LLM client, I want to omit `timezone` from `task.create.v1` and have the server fall back through `X-User-Timezone` header → `MCP_USER_TZ` env → `"UTC"`, so that users with the default configured do not need to repeat it for every task.
9. As an LLM client, I want to override the resolver by passing an explicit `timezone` field, so that "every morning at 9am Tokyo time" works regardless of the default.
10. As an LLM client, I want the resolved `timezone` to be **frozen at create time** in `jobs.timezone`, so that subsequent header changes do not retroactively shift schedules.

### Job chaining (W2)

11. As an LLM client, I want to set `trigger_on_job_id` and `trigger_on_status` on `task.create.v1` to link a downstream `Job` to an upstream `Job`'s terminal status.
12. As an LLM client, I want `trigger_on_status = "SUCCEEDED"` to release only when the upstream `Job`'s run succeeded; anything else (FAILED, CANCELLED) flips the chained run to `CANCELLED`.
13. As an LLM client, I want `trigger_on_status = "ANY"` to release on **any** terminal status of the upstream — including `CANCELLED` — so that cleanup tasks fire unconditionally.
14. As an LLM client, I want `task.create.v1` to reject a chain whose `trigger_on_job_id` doesn't exist, isn't owned by my `user_id`, has already terminated, would form a cycle, or would create a chain deeper than 10.
15. As an LLM client, I want cycle and depth checks performed at create time (not deferred to runtime) so that invalid chains never enter the system.
16. As an LLM client, I want a recurring upstream `Job`'s **first** terminal run to release a one-shot downstream chain once — not on every subsequent occurrence. (Subject to revisit; recorded as an open call in `grilling-state.md`.)

### Cancellation semantics (W2)

17. As an LLM client, I want `task.cancel.v1` on a recurring `Job` to stop future `JobRun` spawning by setting `Job.cancelled_at`.
18. As an LLM client, I want `task.cancel.v1` to flip `PENDING` / `QUEUED` / `WAITING` / `RETRYING` runs to `CANCELLED` immediately.
19. As an LLM client, I want a **currently `RUNNING`** run to complete naturally rather than be aborted — the cancellation is "best-effort for in-flight execution".
20. As an LLM client, I want the tool description to clearly state the best-effort behavior, so that the user understands "the last in-flight run will finish".
21. As an LLM client, I want `task.cancel.v1` semantics to be uniform across `immediate` / `one-shot` / `recurring` schedules so that I do not need to special-case schedule types.

### MCP resources (W2)

22. As an MCP client, I want `resources/list` to expose `tasks://list`, `tasks://actions` (static) and `tasks://job/{job_id}` (template) so I can discover the read-only surface at session start.
23. As an MCP client that auto-attaches static resources, I want `tasks://list` content automatically available to the LLM, so that "what tasks do I have?" can be answered without an explicit tool call.
24. As an MCP client, I want `tasks://actions` content to expose every registered action with its description, timeout, and JSON Schema for `action_params`, so that the LLM does not need to call `task.list_actions.v1` to know how to construct task params.
25. As an MCP client, I want `tasks://list` capped at the newest 20 jobs and the response shape `{items, total, snapshot_at}`, so that token budget for ambient context stays bounded.
26. As an MCP client, I want `tasks://job/{job_id}` for a job belonging to another user to return 404 (not 403), so that cross-tenant existence cannot be enumerated.
27. As an MCP client, I want the existing tools (`task.list.v1`, `task.status.v1`, `task.list_actions.v1`) to remain available alongside resources, so that clients without resources support degrade gracefully and clients needing fresh post-write data can opt out of stale snapshots.

### MCP prompts (W2)

28. As an MCP client user, I want a `daily_review` slash command that produces a message templated to ask Claude to review my scheduled tasks using `tasks://list`, so that I have a turnkey morning workflow.
29. As an MCP client user, I want a `setup_summary` slash command with `topic` and `schedule` arguments producing a message templated to ask Claude to schedule a recurring summarization task, so that I do not need to remember `task.create.v1`'s exact field shape.
30. As an MCP client, I want prompt arguments to be passed as strings (no enum validation server-side) and to receive `INVALID_PARAMS` if a required argument is missing.
31. As an MCP client, I want the prompt text to **reference resource URIs by name** (`tasks://list`, `tasks://actions`), so that the LLM is guided to read context before acting.

### Schema migration (W2)

32. As an operator, I want a single `w2_schema` alembic migration to add `cancelled_at` and drop `raw_user_input` / `parsing_metadata`, so that the schema reflects the post-Session-#3 design without leftover dead columns.
33. As an operator, I want the migration tested with both `upgrade head` and `downgrade -1` in CI, so that the rollback path is verified.

### Acceptance gate (W2)

34. As a reviewer, I want a programmatic E2E test exercising W1's 6 inspector steps **plus** W2's recurring, chaining, resources, and prompts flows, so that "W2 done" is mechanically verifiable in CI.
35. As a reviewer, I want a manual MCP Inspector flow in `docs/W2-VERIFICATION.md` listing ~11 click-through steps so that a human can sanity-check observable behavior.
36. As a reviewer, I want a Claude Desktop sanity check confirming the MCP shows 5 tools, 3 resources, and 2 prompts after `claude mcp add`, so that the LLM-client integration story is end-to-end demonstrable.
37. As a contributor, I want the recorded demo video deferred to W4 polish, so that W2 sprint focuses on bonus implementation rather than scripting / takes / video editing.

### Operational vocabulary upgrades (W2)

38. As a developer, I want `RecurringJobWatcher` and `ChainWatcher` to consume `run_events` via `processed_by` JSONB cursors (already designed in W1), so that downstream reactors never see status without an event and are idempotent across restarts.
39. As a developer, I want a `recursive CTE` in chain validation that doubles as cycle detection (V4) and depth check (V5), so that one SQL pass enforces both invariants.

---

## Implementation Decisions

### D1. Recurring expansion logic (Q-W2-2, Q-W2-4)

**Concurrency policy**: `Forbid` — only one `JobRun` of a recurring `Job` exists in non-terminal states at any moment. This is the natural consequence of "spawn next on terminal event, not on cron schedule": `RecurringJobWatcher` subscribes to terminal `RunEvent`s of recurring `Job`s; when it sees one, it computes the next occurrence and inserts exactly one new `JobRun`. There is no policy field on `Job`; `Forbid` is intrinsic.

**Skipped windows are not recorded as events.** If a run hangs past the next scheduled time, the next occurrence is computed from when the slow run actually terminated (`croniter.get_next(start_time=terminated_at)`), naturally "skipping" the windows it overran. The user-visible signal is the gap in `recent_runs` history plus the slow run's `started_at` vs `completed_at` span.

**Cron evaluation**: `croniter` library with `start_time` carrying the resolved IANA timezone via `zoneinfo.ZoneInfo`. Inclusive first-run semantics are achieved by passing `start_time = now - 1µs` so `get_next()` returns `>= now`.

**DST behavior**: croniter default. Spring-forward (skipped hour) → next valid wall-clock. Fall-back (duplicated hour) → first occurrence only.

**Cron syntax accepted**: 5-field POSIX + `@daily` / `@hourly` / `@weekly` / `@monthly` / `@yearly` shortcuts. Explicitly **not supported**: 6-field with seconds, `@reboot`, `@every Ns`. Validation happens at `task.create.v1`; `RecurringJobWatcher` assumes pre-validated input.

**Spawn batching**: exactly one next `JobRun` per terminal event. Never pre-spawn multiple future occurrences — this keeps cancellation atomic and avoids the "kill a queue of future runs" anti-pattern.

### D2. Cancellation semantics (Q-W2-3)

`task.cancel.v1(job_id)` becomes a **job-level** operation:

1. `UPDATE jobs SET cancelled_at = now() WHERE job_id = :job_id AND cancelled_at IS NULL`.
2. `UPDATE job_runs SET status = 'CANCELLED' WHERE job_id = :job_id AND status IN ('PENDING', 'QUEUED', 'WAITING', 'RETRYING')`; for each affected run, emit a `RunEvent` of type `CANCELLED`.
3. **`RUNNING` runs are not touched**. They complete naturally, emit their natural terminal event (`SUCCEEDED` / `FAILED`), and the worker writes their result as usual. The job remains `cancelled` at the `Job` level because `cancelled_at` is set.
4. `RecurringJobWatcher`'s spawn predicate becomes: `WHERE schedule_type = 'recurring' AND cancelled_at IS NULL` — so no further occurrences spawn for cancelled jobs.
5. Behavior is identical across `schedule_type` values; the cancellation contract is one consistent rule.

**Tool description** explicitly states: *"If a run is currently in progress, it will finish naturally; cancellation only stops future runs."*

**Versioning**: stays `task.cancel.v1` (in-place change). Rationale: no MCP client has cached the W1 schema yet (the project hasn't been connected to any persistent Claude Desktop session), so the version-bump principle's protected scenario doesn't apply. Bumping to `.v2` would create double code paths for a non-existent backward-compatibility concern. Documented in ADR-022.

### D3. Timezone resolver (Q-W2-4)

A new resolver function determines `Job.timezone` at create time using a 4-step chain:

1. Explicit `timezone` field in the `task.create.v1` call.
2. HTTP header `X-User-Timezone` (HTTP transport only).
3. Environment variable `MCP_USER_TZ` (stdio transport mainly; also works for HTTP).
4. Server default `"UTC"`.

The resolved value is written to `jobs.timezone` (NOT NULL) once and not re-resolved on header changes. Invalid `timezone` strings (`zoneinfo.ZoneInfo()` raise) yield a `USER_INPUT` error with `field=timezone, expected="IANA timezone name like 'Asia/Taipei' or 'UTC'"`. The resolver mirrors the `user_id` resolver design (W1 Q15) — same fallback shape, same upgrade path: W3 replaces step 2 with ALB OIDC injection from a Cognito claim.

For one-shot tasks, NL parsers (in client LLMs) are instructed via the system instruction to produce a **tz-aware ISO 8601** value (`2026-05-17T09:00:00+08:00`), so the `timezone` field is **only meaningfully used for recurring jobs**.

### D4. Chain release logic (Q-W2-8)

**Create-time validation (V1-V5)**:

| # | Rule | Failure → error code |
|---|---|---|
| V1 | `trigger_on_job_id` exists | `NOT_FOUND`, field=trigger_on_job_id |
| V2 | Trigger job has same `user_id` as caller | `NOT_FOUND` (intentionally 404 not 403, prevents cross-tenant enumeration) |
| V3 | Trigger job is not already terminated (its last run is non-terminal or it's recurring with `cancelled_at IS NULL`) | `INVALID_STATE` |
| V4 | The proposed chain (back-walk via `trigger_on_job_id`) doesn't reach the new job itself | `USER_INPUT`, field=trigger_on_job_id, expected="non-circular chain" |
| V5 | The proposed chain depth (from new job back through its ancestors) is ≤ 10 | `USER_INPUT`, field=trigger_on_job_id, expected="chain depth ≤ 10" |

V4 and V5 are co-implemented with one recursive CTE that walks the chain ancestors and asserts max depth + non-self-reference. Prototype SQL skeleton:

```sql
WITH RECURSIVE chain AS (
  SELECT job_id, trigger_on_job_id, 1 AS depth
  FROM jobs WHERE job_id = :trigger_id
  UNION ALL
  SELECT j.job_id, j.trigger_on_job_id, c.depth + 1
  FROM jobs j JOIN chain c ON j.job_id = c.trigger_on_job_id
  WHERE c.depth < 11
)
SELECT max(depth) AS max_depth FROM chain;
-- Reject if max_depth >= 10 OR if a recursive expansion sees the new job's would-be id.
```

**Initial `JobRun` for chained jobs**: created with `status = 'WAITING'` and `wait_for_run_id` set to the upstream job's most-recent-or-imminent run. `task.create.v1` for a chained `Job` does not enqueue immediately.

**`ChainWatcher` release algorithm**:

1. Read terminal `RunEvent`s not yet `processed_by["chain"]`.
2. For each event, find `WAITING` runs whose `wait_for_run_id = event.run_id`.
3. For each waiting run, fetch its `Job.trigger_on_status`. Compare against the event's terminal type (`SUCCEEDED` / `FAILED` / `CANCELLED`):
   - `SUCCEEDED` matches only `SUCCEEDED`; else flip to `CANCELLED`.
   - `FAILED` matches only `FAILED`; else flip to `CANCELLED`.
   - `ANY` matches all terminal types **including `CANCELLED`** (least-surprise enum semantics, see ADR-020).
4. Flip the waiting run (atomic UPDATE with `WHERE status = 'WAITING'`) to `PENDING` or `CANCELLED`; emit `RunEvent` of type `QUEUED_BY_CHAIN` or `CANCELLED_BY_CHAIN_MISS`.
5. Mark the event `processed_by["chain"]`.

Recurring upstream + one-shot downstream: the **first** terminal event flips the downstream's `WAITING` → terminal release; subsequent terminal events of the same recurring job are no-op for that downstream (it's already past `WAITING`). To re-fire a chain on every occurrence, the downstream must itself be recurring — chaining is a "one-shot hook" not a "subscription".

### D5. Server-side LLM removed from W2 scope (Q-W2-5, Q-W2-6, Q-W2-7) [β path]

Three coupled decisions collapse to one stance: **W2 does not contain a server-side LLM**.

1. **NL parser**: not implemented. The course bonus *"Connect a real LLM to parse natural language task descriptions"* is reinterpreted as **"connect the MCP server to a real LLM-powered client"** (Claude Desktop, Claude Code, Codex). The MCP design itself — strict JSON schema, version suffixes, structured fixable errors, ≤150-token system instruction — IS the NL parser surface, designed to let the client LLM parse user utterances reliably. Server-side re-parsing would duplicate this work, increase latency, add a paid dependency, and contradict the course-spec's stated assumption ("ChatGPT custom connector").
2. **`llm_summarize` action**: not shipped. The original W1 grilling Q13 added this as an action-registry demo. Without a recurring + automated context (which is exactly what W2 enables), its value is marginal — a user asking for a summary in Claude Desktop already gets one from the client LLM. The action's value emerges only when the server runs autonomously of an open client session (i.e., W3 cloud deployment running 24/7 against an LLM API). Moved to the W4 "행 يوtenir 餘力" backlog as `(D-X)`.
3. **`llm_chat` action**: not shipped. Same rationale as `llm_summarize` but with even weaker demo value.

Consequences:
- No `LLMClient` interface, no `OpenAIClient`, no `OPENAI_API_KEY` env var, no `openai` Python dependency.
- W2 `action` enum stays at `["echo", "http_call"]`.
- W3 deployment does not need NAT Gateway for outbound OpenAI calls (saving $32/month in the default architecture), nor does it need SSM Parameter Store for an API key.
- The "ChatGPT Task Scheduler" project name retains brand alignment via the **client LLM** (any LLM client speaking MCP works — name evokes the user experience).

### D6. MCP resources (Q-W2-9)

Three URIs published via `resources/list`:

| URI | Type | Auto-attached? | Body shape |
|---|---|---|---|
| `tasks://list` | static | client decides | `{items, total, snapshot_at}` — top 20 jobs newest-first, filtered by resolved `user_id` |
| `tasks://job/{job_id}` | template | on-demand | `{job_id, schedule_type, cron_expr, timezone, scheduled_at, cancelled_at, action, action_params, recent_runs[]}` |
| `tasks://actions` | static | client decides | Action registry: name, description, timeout_seconds, params JSON Schema |

**Conventions**:
- URI scheme `tasks://` (plural; complements tool namespace `task.*` singular).
- MIME type `application/json` throughout.
- Cross-user requests return 404 (not 403) to prevent existence enumeration.
- `resources/list` snapshot is taken at session start; the description hint suggests calling tools for fresh post-write data.
- **No subscription support in W2**. The push-update story belongs to W3 / W4 when Claude Desktop's subscription support is more reliable and when a cloud deployment provides genuine push value (mobile notifications).
- The companion tools (`task.list.v1`, `task.status.v1`, `task.list_actions.v1`) remain available — clients without resource support degrade gracefully; clients needing fresh post-write data opt into tool calls.

### D7. MCP prompts (Q-W2-10)

Two light prompts (server returns template text only; data fetching happens in the client LLM via tools/resources):

| Prompt | Args | Template body summary |
|---|---|---|
| `daily_review` | none | "Review my scheduled tasks. Use the `tasks://list` resource for what's pending, then list each task's status and next run time. Highlight failed runs in the last 24 hours." |
| `setup_summary` | `topic` (string, required), `schedule` (string, required) | "Schedule a recurring task. Topic: {topic}. Schedule: {schedule}. Use the `tasks://actions` resource to verify parameter schemas, then call `task.create.v1` with the appropriate cron_expr." |

**Conventions**:
- Args are stringly-typed (MCP prompt limitation); server validates non-empty only.
- Missing required args yield `INVALID_PARAMS` per MCP spec.
- Prompts **explicitly reference** resource URIs, strengthening the prompts ↔ resources story.
- English only in W2; localized prompts deferred to W4 polish.
- No third prompt (e.g., `investigate_failures`) — bonus completion does not require quantity.

### D8. System instruction (Q-W2-13)

`app/mcp/system_instruction.md` ships at ~145 tokens, read by the MCP server at boot and injected into `serverInfo`. Final text:

```
You are scheduling tasks via the task-scheduler MCP.

Available actions: echo (test/reminder), http_call (POST/GET to URL).
Each tool returns {ok, data|error}. Honor error.expected hints.

Defaults when unspecified: schedule_type="immediate". For one-shot/
recurring, server resolves timezone from headers or env (UTC fallback).

For recurring jobs, prefer @daily/@hourly shortcuts. Each recurring job
runs sequentially (next run only spawns after previous terminates).

To chain jobs, set trigger_on_job_id with trigger_on_status (SUCCEEDED|
FAILED|ANY). The chained job's first run waits for the trigger.

Use task.cancel.v1 to stop a job. If a run is currently in progress,
it will finish naturally; cancellation only stops future runs.

Ask one clarifying question only if essential info is missing.
```

The instruction mentions only **tools-visible information** (action types, scheduling defaults, cron shortcuts, cancellation contract). It deliberately does **not** mention resources or prompts — clients discover those via their own `resources/list` and `prompts/list` calls, and adding them inflates the token budget without LLM benefit.

### D9. Schema migration (Q-W2-12)

A single alembic migration `w2_schema`:

```sql
-- upgrade
ALTER TABLE jobs ADD COLUMN cancelled_at TIMESTAMPTZ NULL;
ALTER TABLE jobs DROP COLUMN raw_user_input;
ALTER TABLE jobs DROP COLUMN parsing_metadata;

-- downgrade
ALTER TABLE jobs ADD COLUMN parsing_metadata JSONB NULL;
ALTER TABLE jobs ADD COLUMN raw_user_input TEXT NULL;
ALTER TABLE jobs DROP COLUMN cancelled_at;
```

No new indexes (W2 query patterns are covered by the W1 `idx_jobs_user_created` index plus a `WHERE cancelled_at IS NULL` predicate when needed). No partition changes (`job_runs` declarative `PARTITION BY RANGE` moves to W3 alongside RDS).

Migration tested by `tests/integration/test_alembic_migration.py` exercising both `upgrade head` and `downgrade -1` against a clean Postgres.

### D10. `task.create.v1` schema additions (D4, D2)

Two optional fields added to the existing W1 schema; `additionalProperties: false` stays true (W1 constraint). Both nullable, both invalid `null`-state allowed.

```jsonc
{
  "trigger_on_job_id": {
    "type": ["integer", "null"],
    "description": "If set, this job's first run waits until the referenced job reaches a terminal status."
  },
  "trigger_on_status": {
    "type": "string",
    "enum": ["SUCCEEDED", "FAILED", "ANY"],
    "default": "SUCCEEDED",
    "description": "Which terminal status of trigger_on_job_id releases this job's run."
  }
}
```

The W1 `timezone` field becomes effectively optional from the **input** perspective (the resolver supplies a value); `jobs.timezone` remains NOT NULL at the DB level. No `oneOf` for `action_params` — the action handler's Pydantic `params_model` enforces the inner shape at dispatch.

### D11. LLM bonus reinterpretation (Q-W2-6) [ADR-019]

The course-spec lists *"Connect a real LLM to parse natural language task descriptions before calling task.create"* as one of five bonus items. Session #3 grilling re-reads this in light of two facts:

1. The course spec **explicitly assumes** a ChatGPT custom connector exists: *"假設我們已經有 ChatGPT custom connector, 請設計一個系統, 能夠支援 LLM ChatBot 在指定時間排程執行 jobs."* — i.e., the LLM is the **caller**, not the **callee**.
2. The course-spec § 7 (MCP integration reliability principles) is **entirely** about making the schema easy for client LLMs to parse correctly — version suffixes, strict JSON Schema, ≤150-token system instruction, structured fixable errors. The MCP design **is** the LLM-NL surface.

We therefore interpret "Connect a real LLM" as: **the MCP server is connected to a real LLM the moment a Claude Desktop / Claude Code / Codex client connects to it**. The NL parsing happens in that client LLM, leveraging the careful schema design. This satisfies the bonus without duplicating work in a server-side LLM call.

The acceptance gate L3 step `claude mcp add task-scheduler ...` is the concrete demonstration: an MCP server connected to a real LLM client (Claude Desktop), executing tasks expressed in natural language by the user.

### D12. Acceptance gate layers (Q-W2-14)

Three layers, in order:

**L1 — CI E2E test**: extends `tests/integration/test_e2e_inspector_flow.py` with W2 steps. 10 steps total (W1's 6 + W2's 4): create recurring → assert RecurringJobWatcher spawn next → cancel recurring → assert no further spawn; create A → create B chained → assert B's WAITING → A succeeds → assert B flips PENDING and runs; `resources/list` returns 3 entries → `resources/read tasks://list` returns the user's jobs; `prompts/list` returns 2 entries → `prompts/get setup_summary` with args returns templated message. Watcher logic exercised by direct function invocation (`RecurringJobWatcher.tick()`, `ChainWatcher.tick()`), not by waiting on real cron clock; CI does not sleep for cron.

**L2 — Manual MCP Inspector flow**: `docs/W2-VERIFICATION.md` lists ~11 click-through steps to validate observable behavior. Uses real `npx @modelcontextprotocol/inspector` against the running stack. ~5 minute manual check.

**L3 — Claude Desktop sanity check**: `claude mcp add task-scheduler ...` connects the MCP to Claude Desktop; verify the 🔨 icon shows 5 tools, the resources tab shows 3 entries, the prompts tab shows 2 entries. ~2 minute manual check; no recording.

**L4 — Demo video (3 min)**: **moved to W4 polish**. Recording deserves the cloud deployment story (ALB DNS, CloudWatch dashboard) for stronger portfolio signal; W2 sprint is for implementation, not media production.

### D13. Tier scoping and W2 cuts (Q-W2-1, Q-W2-11, Q-W2-15) [ADR-023]

After β collapsed the LLM work, Tier 1 became cron + chaining (2.5 days). Tier 2 = MCP resources + prompts (1.2 days). Tier 3 = everything else, **all cut** for W2:

- `send_email` action: deferred to W4 as `(D-X)` (best demonstrated against real AWS SES in deployment).
- `llm_summarize` / `llm_chat` actions: deferred to W4 as `(D-X)` (per D5).
- Third MCP prompt (e.g., `investigate_failures`): not shipped (bonus completion is qualitative, not by count).
- `job_runs` declarative `PARTITION BY RANGE`: deferred to W3, where it pairs naturally with RDS storage management and `pg_partman` automation.
- W2 demo video: deferred to W4 polish.

The freed budget (~1 day from the Tier 1 cut + Tier 3 elimination) is allocated to: extra testing coverage, the `docs/W2-VERIFICATION.md` write-up, and an early start on W3.

---

## Testing Decisions

### What makes a good test (here, extending W1)

- **Test external behavior, not implementation**: `RecurringJobWatcher.tick()` after inserting a terminal `RunEvent` should produce a new `JobRun` with the expected `scheduled_at` — don't assert which SQL was emitted.
- **Test the seams that matter**: cron expansion math (does `croniter.get_next` with the right `start_time` produce the expected next-run?), chain release logic (does `ChainWatcher` correctly select PENDING vs CANCELLED based on `trigger_on_status` ∩ event type?), cancellation atomicity (RUNNING untouched while other runs flip to CANCELLED), schema migration round-trip (`upgrade head` then `downgrade -1` then `upgrade head` again).
- **Don't mock the database**. Integration tests run against the real Postgres in Docker Compose. The mutation patterns we care about (atomic `UPDATE ... WHERE status = ...`, recursive CTE results, `processed_by` JSONB update via `||` operator) only exhibit their real semantics against Postgres.
- **Mock the network and the system clock** — but the system clock only via dependency injection of "now" into the cron evaluator, not by patching `datetime.utcnow()`. Real timestamps everywhere; the cron evaluator just takes `start_time` as a parameter.
- **Do not sleep for real cron tick** in CI. Direct invocation of `RecurringJobWatcher.tick()` is faster and more reliable than waiting 75 seconds for a `*/1 * * * *` to fire.

### Modules with W2 test coverage targets

| Module | Test type | What's covered |
|---|---|---|
| `app/config/cron.py` (croniter wrapper) | Unit | `next_after(cron_expr, tz, start)` correctness; inclusive `>=` semantics; DST spring/fall behavior; invalid expression raises ValueError with field hint |
| `app/domain/chain_validation.py` | Unit (in-memory or mock-DB) | V1-V5 each: produces the correct error code and field; recursive CTE behaves correctly for cycle and depth |
| `app/workers/recurring_watcher.py` | Integration | Insert recurring `Job` + a terminal `RunEvent`; `tick()` once; assert new `JobRun` with correct `scheduled_at`; insert another terminal event after setting `cancelled_at`; `tick()` again; assert no new `JobRun` |
| `app/workers/chain_watcher.py` | Integration | All 9 combinations of (event terminal type) × (trigger_on_status): assert correct flip (PENDING vs CANCELLED); assert idempotency (calling `tick()` twice doesn't double-flip) |
| `app/domain/jobs.py` (cancel path) | Integration | Cancel one-shot in PENDING → CANCELLED + `cancelled_at` set; cancel recurring with one PENDING + one RUNNING run → PENDING flips, RUNNING untouched, `cancelled_at` set; cancel already-cancelled job → no-op (idempotent or INVALID_STATE per design) |
| `app/mcp/resources/*` | Integration | `resources/list` returns 3 entries with correct templates; `resources/read tasks://list` filters by user_id; `resources/read tasks://job/{99}` for non-existent or cross-user → 404 |
| `app/mcp/prompts/*` | Integration | `prompts/list` returns 2 entries; `prompts/get daily_review` returns expected template text; `prompts/get setup_summary` with args substitutes; missing required arg → `INVALID_PARAMS` |
| `alembic/versions/xxx_w2_schema.py` | Integration | `upgrade head` then introspect schema (cancelled_at exists, raw_user_input doesn't, parsing_metadata doesn't); `downgrade -1` then introspect (reverse); third `upgrade head` re-applies cleanly |
| `tests/integration/test_e2e_inspector_flow.py::test_w2_bonuses` | Integration | End-to-end W1 6 steps + W2 4 steps via in-process MCP server invocations (`httpx.ASGITransport` for MCP calls, direct watcher invocations for time-advance) |

### Coverage target

80%+ maintained from W1, enforced by `pytest-cov` locally (CI gate ships in W4).

### Prior art

- W1's `tests/integration/test_e2e_inspector_flow.py` — the pattern for in-process MCP server testing + direct `claim_and_publish` / `process_one` invocations to fast-forward the scheduling pipeline.
- Oban (Elixir) and `graphile-worker` (Postgres + Node) — public reference implementations of `FOR UPDATE SKIP LOCKED` claim semantics and `processed_by` cursor pattern over an event outbox; their docs describe edge cases (concurrent watchers, stuck claims) we cross-check our implementations against.
- `croniter` test suite — DST edge cases are heavily tested by the library itself; our `app/config/cron.py` tests focus on **our wrapping invariants** (inclusive first-run, error-shape for invalid input), not croniter's correctness.

---

## Out of Scope

These belong to later weeks (W3 / W4) or the future-upgrade list:

### Out of scope for W2, in scope for W3

- Terraform modules for ECS Fargate / RDS / ElastiCache / ALB / SQS / IAM / VPC.
- Network topology choice (P1 public-subnet vs P2 NAT Gateway vs P3 Bedrock + VPC Endpoint).
- GitHub Actions CI/CD: lint, test, build, push to ECR, ECS deploy.
- AWS Budgets alert + IAM least-privilege roles.
- ALB OIDC integration replacing step 2 of the user_id and timezone resolvers.
- RDS Proxy or pool downsizing decision (W1 PRD raised the 130 vs 81 connection conflict).
- `job_runs` declarative `PARTITION BY RANGE` + `pg_partman` automation cron.

### Out of scope for W2, in scope for W4

- CloudWatch metrics, dashboards, alarms.
- Structured JSON logging.
- 3-minute demo video (the L4 layer of the acceptance gate).
- Blog post + README polish + architecture diagram.
- Localized prompts (Mandarin / other languages).
- MCP resource subscription (`resources/subscribe` + push notifications).
- Lambda-based worker variant for trade-off blog comparison.

### Out of scope for W2, in scope for W4 "行有餘力" backlog (D-X)

- `send_email` action with real SES integration (sandbox first, then verified-sender production).
- `llm_summarize` action with OpenAI / Bedrock / Anthropic integration (the deferred β-cut item).
- `llm_chat` action.
- Bedrock swap (replace any future OpenAI dependency with Bedrock + VPC Endpoint).
- MCP `sampling` capability for client-LLM-mediated server tasks.
- Audit-trail JSONB field on `jobs` (re-introducing `raw_user_input` equivalent in a generic shape).

### Permanently out of scope (would change the project's identity)

- DAG-based job dependencies (chaining is linear by design).
- Dynamic action plugin loading.
- Multi-region active-active deployment.
- Real OAuth / API key issuance from this service.
- Web UI (this is a backend portfolio piece).

---

## Further Notes

### Companion documents (must-read for implementers)

- **`.doc/session/grilling-state.md`** — decision ledger Q-W2-1 to Q-W2-16, single source of truth for "why each thing is the way it is".
- **`docs/PRD/prototype-w1.md`** — the W1 PRD this one builds on; required for understanding existing entity model and process roles.
- **`CONTEXT.md`** — domain glossary; update inline as W2 features ship.
- **`.doc/learn/system-design.md`** — schema, 4-week plan, upgrade backlog.
- **`.doc/learn/course-spec.md`** — course material structured + our deviations.
- **`PROMPT.md`** — original course assignment + verification checklist.
- **`docs/W2-VERIFICATION.md`** — manual MCP Inspector flow (to be created as part of W2 acceptance gate).

### ADRs to write during W2 implementation

| ADR | Title | Captures |
|---|---|---|
| ADR-016 | cron-semantics | Forbid concurrency policy, DST behavior, 5-field POSIX + shortcuts, no seconds |
| ADR-017 | timezone-resolver | 4-step fallback chain, W3 ALB OIDC upgrade path |
| ADR-018 | no-server-side-llm-in-w2 | β path rationale: client LLM is sufficient, server-side LLM moves to W4 |
| ADR-019 | llm-bonus-via-client-llm-integration | Reinterpretation of "Connect a real LLM" bonus |
| ADR-020 | chain-watcher-validation | 5 validation rules, recursive CTE, `ANY` includes CANCELLED |
| ADR-021 | acceptance-gate-layering | L1 CI / L2 manual / L3 sanity / L4 deferred to W4 |
| ADR-022 | cancel-semantics-best-effort | RUNNING untouched + `cancelled_at` + uniform across schedule types + `.v1` in-place |
| ADR-023 | tier-scoping-and-w2-cut-scope | Why `llm_summarize` / `send_email` / NL parser / W2 partition all moved out |

### Verification (echoes Q-W2-14)

W2 is "done" when these pass:

1. **L1 (CI gate)**: `uv run pytest -m integration tests/integration/test_e2e_inspector_flow.py::test_w2_bonuses` and all other W2-specific integration tests are green.
2. **L2 (Manual gate)**: All 11 steps in `docs/W2-VERIFICATION.md` produce the expected observable outputs.
3. **L3 (Claude Desktop sanity)**: `claude mcp add task-scheduler ...` succeeds, the connection appears in `claude mcp list` as `✓ Connected`, and the Claude Desktop tools / resources / prompts panels show the correct cardinalities (5 / 3 / 2).

L4 (3-minute demo video) ships in W4 polish alongside cloud-deployment visuals.

### Risks and mitigations

- **Risk: croniter's DST handling diverges from user expectations.** Mitigation: ADR-016 documents the chosen behavior with worked examples; `tests/unit/test_cron_expansion.py` covers spring-forward + fall-back + Samoa-style TZ rule shifts.
- **Risk: ChainWatcher and RecurringJobWatcher race when both consume the same terminal event.** Mitigation: `processed_by` JSONB uses distinct keys (`chain`, `recurring`) — both watchers process independently, neither blocks the other. The `||` JSONB merge is atomic at row level.
- **Risk: long-running `RUNNING` runs block recurring job spawning indefinitely (Forbid policy).** Mitigation: per-action `timeout_seconds` via `asyncio.wait_for` + reconciler grace window from W1 — both already in place, both write terminal events that RecurringJobWatcher consumes to resume.
- **Risk: MCP client (Claude Desktop) varies in resource auto-attach support.** Mitigation: keep tools alongside resources; L3 acceptance gate verifies the specific version of Claude Desktop being used; document graceful degradation in `tasks://list` description ("Snapshot taken at conversation start").
- **Risk: dropped columns (`raw_user_input`, `parsing_metadata`) discovered as needed in W3+.** Mitigation: alembic `downgrade -1` is straightforward (the migration is reversible); ADR-023 documents the original intent and re-introduction path.
- **Risk: chain depth limit (10) feels arbitrary and rejects valid use cases.** Mitigation: `expected` field in the error message tells the LLM client the limit; raising it is one constant change; no production-traffic justification for tuning in W2.

### Decision provenance

Every decision in this PRD traces to:

- A `Q-W2-#` entry in `.doc/session/grilling-state.md` (this conversation's decision log), or
- A section of `.doc/learn/course-spec.md` (course material we adopted or reinterpreted), or
- The W1 PRD `docs/PRD/prototype-w1.md` (the surface this W2 implementation extends).

If a future implementer disagrees with any decision, that's the audit trail to revisit.

---

*Generated 2026-05-16 from `.doc/session/grilling-state.md` (Q-W2-1 to Q-W2-16) and W1 PRD via `/to-prd` after Grilling Session #3.*
