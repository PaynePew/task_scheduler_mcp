# ADR-037: `tasks://recent-results` MCP Resource as Briefing Surface

- **Status**: Accepted
- **Date**: 2026-05-19
- **Deciders**: PaynePew
- **Source**: W4 Action Sprint — Grilling Session #5 decision; implemented in issue #95.
- **Related**: ADR-014 (MCP tool surface v1), ADR-015 (user identity resolver), ADR-006 (dual stdio/HTTP transport)

## Context

The task scheduler fires jobs on a cron or one-shot schedule while the user's
LLM client (Claude Desktop, a custom MCP host) may be closed. When the user
reopens the client, the LLM has no automatic way to know what ran overnight.
This is the **silent-success loop**: jobs completed, results were stored, but
the LLM cannot proactively brief the user because it has never been asked.

MCP clients surface `resources/list` on connect. This creates a pull-based
briefing hook: if a resource containing the recent run history exists, the
client can retrieve it on session start and use it as context for an
unprompted summary: "Since you were last online, 3 jobs completed successfully
and 1 failed with a timeout error."

The alternative — a push notification system — would require coupling the
scheduler to a specific delivery sink (email, Slack, webhook). That coupling
is outside the current scope and creates operational risk (secret management,
rate limits, bounce handling). The client-pull paradigm matches MCP's
resource model and imposes no external dependencies.

## Decision

Add a new static MCP resource at `tasks://recent-results` that:

1. **Queries `JobRun`** joined to `Job` for `user_id` + `action`, filtering
   for the calling user's runs where `finish_at >= NOW() - 24h` and `status`
   is one of `SUCCEEDED`, `FAILED`, `CANCELLED`.
2. **Orders by `finish_at DESC`** so the most recent activity appears first.
3. **Caps at 50 rows** to prevent context-window bloat in downstream LLMs.
4. **Returns a structured JSON payload**:
   ```json
   {
     "queried_at": "<iso8601>",
     "user_id": "<uid>",
     "window_hours": 24,
     "runs": [
       {
         "job_id": 42,
         "run_id": 7,
         "action": "echo",
         "status": "completed",
         "start_at": "<iso8601>",
         "finish_at": "<iso8601>",
         "error_excerpt": null
       }
     ]
   }
   ```
5. **Resolves `user_id`** via the existing ADR-015 resolver chain (MCP_USER_ID
   env var → ALB OIDC sub claim in W3+), so no new identity machinery is needed.

`resources/list` now returns **4 entries**: `tasks://list`, `tasks://actions`,
`tasks://job/{job_id}` (template), and `tasks://recent-results`.

## Rationale

### Why the 24h window?

24 hours matches the typical "since I was last online" expectation for a daily
user. A shorter window (e.g. 1h) would miss overnight batch jobs; a longer
window (7 days) would surface noise from much older runs and bloat the payload.
The `window_hours` field is included in the response so the LLM can accurately
describe the window to the user ("here's what ran in the last 24 hours").

### Why the 50-row cap?

Each row serialises to roughly 200–400 bytes of JSON. 50 rows ≈ 10–20 KB,
well within a typical context window. Beyond that, the cost (context tokens)
outweighs the benefit (marginally older run history). The SQL `LIMIT 50` is
enforced at the query layer; a Python-level slice provides defense in depth
against the LIMIT being accidentally removed.

### Why not push notifications?

Push requires choosing a specific delivery sink: email (SMTP), Slack
(webhook), webhook (custom endpoint). Each choice adds:
- Secrets management (SMTP credentials, webhook URLs)
- Delivery failure handling (bounces, retries, dead-letter)
- Coupling to external services that may be unavailable in dev/test

Client-pull via MCP resources is transport-agnostic, requires no additional
secrets, and works identically in local dev and production.

### Why only terminal statuses?

`PENDING`, `QUEUED`, `WAITING`, `RUNNING`, and `RETRYING` represent in-flight
work that has not yet produced a result. Including them in a "recent results"
resource would be misleading — the user wants to know what *finished*, not
what is *still running*. In-flight jobs are surfaced by `tasks://list` and the
`task.status.v1` tool.

## Consequences

**Positive:**
- LLM clients can brief users on overnight activity with no user prompting.
- No new external dependencies or secrets required.
- `resources/list` grows from 3 to 4 entries — clients must handle an
  updated list gracefully (most MCP clients iterate the list, so this is
  backwards-compatible).

**Negative / trade-offs:**
- The 24h window and 50-row cap are hardcoded constants. Future requirements
  (longer windows, per-user caps) would require a parameter or config value.
- The resource is pull-only; a user who never refreshes the resource after
  reconnecting will miss the briefing. This is acceptable for the current
  audience (Claude Desktop, which surfaces resources on connect).
- Cross-timezone "last 24h" is always UTC-anchored. A user in UTC+12 who
  works a 9-to-5 schedule will see a different set of "overnight" jobs than
  they might expect. The `queried_at` field lets the LLM communicate this
  anchor precisely.
