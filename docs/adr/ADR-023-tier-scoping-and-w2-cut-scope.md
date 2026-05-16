# ADR-023: W2 cut scope — what shipped, what was deferred, and why

**Status:** Accepted  
**Date:** 2026-05-16  
**Deciders:** PaynePew  
**Refs:** Issue #41, PRD prototype-w1.md, ADR-022 (cancel semantics)

---

## Context

After W1 shipped a working task scheduler (create / status / cancel / list over
MCP, backed by Postgres + SQS/ElasticMQ), the W2 planning pass surfaced several
features that were initially in scope. This ADR records which features landed in W2,
which were deferred, and the rationale for each deferral, so future contributors
understand the intentional gaps.

---

## What shipped in W2

| Feature | Slice |
|---|---|
| `cancelled_at` column + best-effort cancel semantics | #41 (this slice) |
| Drop NL-parser columns (`raw_user_input`, `parsing_metadata`) | #41 (this slice) |
| `task.cancel.v1` idempotent re-cancel | #41 (this slice) |
| Migration roundtrip integration test | #41 (this slice) |

---

## What was deferred and why

### Server-side LLM removed from scope

**Original plan:** An LLM running inside the MCP server would parse natural
language user input (e.g. "send me a reminder tomorrow at 9am") into structured
`action` / `action_params` / `scheduled_at` fields.

**Decision:** Removed entirely. The MCP protocol is already an LLM-to-server
boundary — the client LLM (ChatGPT, Claude, etc.) does the parsing before calling
the tool. Adding a second LLM call server-side duplicates work, adds latency, and
creates a dependency on an inference backend that is out of scope for the W2
prototype. `raw_user_input` and `parsing_metadata` columns, scaffolded in W1 for
this feature, are dropped in migration 0003.

### `send_email` action deferred to W4

**Original plan:** An `email` action type would let users schedule email
notifications via SES.

**Decision:** Deferred to W4. The W2 prototype validates the scheduling and
cancellation mechanics with the simpler `echo` action. Adding SES integration
(IAM role, verified sender, bounce handling) before the core scheduler is
battle-tested would increase the blast radius of each iteration. W4 is the right
time once the scheduler is stable.

### `job_runs` table partitioning deferred to W3

**Original plan:** `job_runs` would be converted to a native `PARTITION BY RANGE`
Postgres table in W2, using `time_bucket` as the partition key.

**Decision:** Deferred to W3. The `time_bucket` column and composite PK were
already scaffolded in W1 (ADR-009) so the app-layer hot path stays migration-free
when partitioning lands. The prototype load (a handful of test jobs) does not
justify the operational complexity of partition management. W3 will introduce
partitioning alongside the load-testing story.

### Demo video deferred to W4

**Original plan:** A short product demo video would accompany the W2 release.

**Decision:** Deferred to W4. The scheduler surface is still evolving (recurring
jobs, send_email, partitioning all pending). Recording a demo against an incomplete
feature set creates stale documentation. W4 is the natural checkpoint once the full
β path is stable.

---

## β adoption path

The W2 scheduler is now the β candidate for internal team use:

1. W1 proved the MCP protocol integration and basic job lifecycle.
2. W2 hardens cancel semantics, drops dead schema, and adds migration safety tests.
3. W3 adds recurring job execution and table partitioning.
4. W4 adds `send_email`, demo video, and public β release.

Each wave is independently deployable; the MCP tool surface (`task.*.v1`) does not
break between waves unless a `.v2` is introduced (which requires cross-client
co-ordination per ADR-014).
