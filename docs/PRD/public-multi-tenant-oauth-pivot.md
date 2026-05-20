# PRD: Public, multi-tenant, OAuth-delegated MCP task scheduler

> Issue: [#129](https://github.com/PaynePew/task_scheduler_mcp/issues/129)
> Source decisions: ADR-049 through ADR-059. Domain vocab: `CONTEXT.md`.
> Incident/overload notes: `.doc/learn/overload-and-incident-response.md`.

## Problem Statement

Today the scheduler is a single-operator tool wearing a product's clothes. The live
VPS exposes an MCP endpoint with **trust-only auth** — anyone who sends an
`X-User-Id` header is believed — and every secret-using action draws on the
**operator's own** credentials from the server environment. So it cannot be safely
shared: a stranger could impersonate any user, exfiltrate the operator's API tokens
in one line via `http_call`, or spend the operator's Slack / email / storage
accounts. The operator wants a **showable product that anyone can connect to and
use safely** — even if few people use it — with low-friction auth and real
protections, not just a personal demo.

## Solution

Pivot to a **public, multi-tenant MCP scheduler authenticated via OAuth 2.1
delegation**. WorkOS AuthKit is the authorization server; our MCP server is a
resource server, so `user_id` is a cryptographically verified identity, not a
self-asserted header. Each public user connects **their own** GitHub / Slack /
Google accounts through familiar "Connect" buttons, so actions run with the user's
own **scoped, revocable** OAuth tokens — the system **never stores a stranger's raw
secret**. AI features (summarize / polish) are offered as **fixed-prompt typed
actions funded by the operator's key**, bounded by hard cost caps. Operator-only
power features (arbitrary `http_call`, SMTP, R2) remain available to the operator
via the existing env-secret mechanism. Per-user and global quotas, structured
queryable logging, and overload protection keep the $5 VPS safe under load.

## User Stories

**Authentication & onboarding**
1. As a public user, I want to add the scheduler as an MCP connector in my client (Claude / ChatGPT / Claude Code) and log in via a browser, so that I can start without manual app registration.
2. As a public user, I want my identity verified by OAuth, so that no one else can see or control my jobs.
3. As a public user, I want to connect my GitHub / Slack / Google accounts through a "Connect" button, so that I never paste raw API keys.
4. As a public user, I want one place to view, manage, and revoke my connections, so that I stay in control of access.
5. As a public user, when a job needs a connection I haven't set up, I want the chatbot to hand me a direct link to connect, so that I'm not stuck hunting for the dashboard.
6. As a public user, I want to be able to revoke access from GitHub / Slack / Google's own settings too, so that I'm never locked into the scheduler.
7. As the operator, I want to keep using the scheduler locally over stdio without OAuth, so that my own workflow stays low-friction.
8. As the operator, I want my identity consistent across stdio and OAuth, so that my jobs and exemptions line up.

**Scheduling core (now per verified user)**
9. As a public user, I want to schedule a one-shot job at a future time, so that it runs when I need it.
10. As a public user, I want to schedule a recurring job with a cron expression and timezone, so that it repeats automatically.
11. As a public user, I want to schedule an immediate job, so that it runs as soon as a worker picks it up.
12. As a public user, I want to view the status of a job, so that I know whether it ran.
13. As a public user, I want to list and filter my jobs by status/time, so that I can find them.
14. As a public user, I want to cancel a job, so that it stops running.
15. As a public user, I want my jobs strictly isolated from other users', so that no one can read or cancel mine.
16. As a public user, I want to chain jobs (A → B on terminal status), so that I can build simple workflows.

**OAuth-backed actions**
17. As a public user, I want a GitHub digest action that uses my GitHub connection, so that I can summarize my activity.
18. As a public user, I want to post to my Slack via my Slack connection, so that I receive results where I work.
19. As a public user, I want to send email from my Gmail via my Google connection, so that I can deliver results by email without SMTP setup.
20. As a public user, I want to schedule "every Friday 08:00, fetch my GitHub issues → email/Slack them", so that I get a weekly summary hands-free.
21. As a public user, I want an `echo` action, so that I can test scheduling without any connection.

**AI actions (operator-subsidized)**
22. As a public user, I want an AI summarize action, so that digests and fetched content are condensed.
23. As a public user, I want an AI polish action, so that my text reads better.
24. As a public user, I want to choose summary style / length / language via simple options, so that the output fits my need.
25. As the operator, I want AI usage capped per user and globally, so that strangers cannot drain my LLM budget.
26. As the operator, I want a pinned cheap model, so that AI costs stay low.
27. As the operator, I want a hard global monthly spend ceiling, so that the worst case is bounded even if other caps fail.

**Operator-only power**
28. As the operator, I want arbitrary `http_call` available only to me, so that the SSRF / exfiltration surface is never public.
29. As the operator, I want SMTP and R2 actions available to me via env secrets, so that I keep full power for my own ops.
30. As the operator, I want every secret-using action blocked for public users, so that no one can spend my accounts.

**Containment & abuse**
31. As the operator, I want per-user job-creation rate limits, so that a runaway loop or robot can't flood the system.
32. As the operator, I want a per-user cap on active recurring jobs, so that permanent steady-state load stays bounded.
33. As the operator, I want a per-user cap on total active jobs, so that one user can't hoard the box.
34. As the operator, I want a global ceiling on active recurring jobs, so that the single core is protected.
35. As the operator, I want to be exempt from all these caps, so that my own usage isn't throttled.
36. As a public user, I want a clear error with a retry-after hint when I hit a limit, so that I can back off correctly.

**Overload protection**
37. As the operator, I want load shedding (503) when the box is unhealthy, so that it degrades gracefully instead of crashing.
38. As the operator, I want concurrency limiting, so that the single core isn't thrashed by too many in-flight requests.
39. As the operator, I want backpressure (429) when the queue is deep, so that clients slow down instead of piling on.

**Observability**
40. As the operator, I want structured JSON logs I can query in Better Stack, so that I can diagnose incidents instead of grepping a dying host.
41. As the operator, I want per-user / per-job / per-run correlation in logs, so that I can trace one user's activity.
42. As the operator, I want secrets and tokens never written to logs, so that logs aren't a leak surface.

**Security & secret custody**
43. As a public user, I want the system to never store my raw long-lived secrets, so that a breach can't leak my passwords.
44. As the operator, I want stored OAuth tokens encrypted via KMS envelope encryption, so that a DB leak doesn't expose them.
45. As a public user, I want my downstream tokens scoped and auto-refreshed, so that access is limited and stays current.

**Rollout**
46. As the operator, I want my existing `default-user` jobs migrated to my identity, so that nothing is lost in the pivot.
47. As the operator, I want the public HTTP endpoint to require OAuth while my stdio stays trusted, so that the switch doesn't break my own usage.

## Implementation Decisions

**Authentication (ADR-053, ADR-049)**
- WorkOS AuthKit = Layer-1 authorization server. The MCP server is an OAuth 2.1 **resource server**; it does not run an AS.
- `user_id` = verified token `sub`. Token validation verifies the JWT against the WorkOS JWKS and checks audience / `resource` binding (RFC 8707) to prevent confused-deputy / token passthrough.
- Expose Protected Resource Metadata (RFC 9728) + a `WWW-Authenticate` 401 challenge pointing at WorkOS. Client onboarding via CIMD (default since the 2025-11-25 spec) with DCR fallback.
- `user_id` resolver order: OAuth `sub` (HTTP) → `MCP_USER_ID` (operator stdio) → reject on public HTTP. Trust-only `X-User-Id` is removed from public HTTP.

**Dual credential model (ADR-050)**
- Public users: per-user **OAuth connections** to GitHub / Slack / Google.
- Operator: `${VAR}` env substitution (ADR-032, unchanged) — operator's own keys only.
- The system never stores a public user's raw long-lived secret.

**Connection store + crypto (ADR-054, ADR-050)**
- `connections/store` keyed by `(user_id, provider)`: `get_fresh_token` (with refresh), `upsert`, `delete`, `list`.
- Tokens encrypted at rest via **AWS KMS envelope encryption** (per-write data key; CMK material never leaves KMS; IAM access key in `.env` for the VPS). ~$1/mo.

**Action surface tiering (ADR-051)**
- `ActionHandler` gains `requires_operator: bool` and `credential_mode` ∈ {`none`, `oauth_connection`, `operator_env`}.
- Tiers: public no-secret (`echo`); public OAuth-backed (`github_digest`, `slack_post`, `email_send` via Gmail, calendar via Google); public operator-funded (`llm_summarize`, `llm_polish`); operator-only (`http_call`, SMTP `email_send`, `r2_upload`).
- `github_digest` / `slack_post` / `email_send`(Gmail) are rewritten to resolve credentials from `connections/store` by `job.user_id` + provider instead of `${VAR}`.
- `r2_upload` is demoted to a VPS cron / shell script, off the MCP action surface.
- `task.create` rejects `requires_operator` actions for non-operator callers.

**Operator-subsidized LLM actions (ADR-052)**
- `llm_summarize`, `llm_polish`: typed handlers, each with ONE fixed operator-authored system prompt (code constant). User/fetched content is passed in the user message as data; the system prompt instructs the model to treat it as data, not instructions.
- Constrained params (Pydantic, `extra="forbid"`, enums + defaults), e.g. `llm_summarize`: `{from_run_id?, text?, style: bullet|paragraph, length: short|medium|long, language, focus[]}`; exactly one of `from_run_id`/`text`.
- Provider-agnostic LLM client; pinned cheap model via env.
- Four caps: pinned model, `max_output_tokens`, input-size cap (truncate/reject before the call), per-user daily token budget + global monthly hard ceiling (provider dashboard hard stop + app-side counter).
- Chain-fed via `from_run_id` (ADR-033).

**Quotas / containment (ADR-055, revising ADR-042)**
- At `task.create`, enforce per verified `user_id`: creation rate (default 100/day, 5/min), active recurring ≤ 5, active total ≤ 50, and a global active-recurring ceiling (default 500) beyond which creation is rejected.
- Operator (`OPERATOR_USER_ID`) is exempt. All limits env-configurable.
- Limit responses carry `retry_after`; missing-connection responses carry `connect_url`.

**Overload protection (ADR-057)**
- `overload/health.should_shed()` from CPU / RAM / queue depth → `503 + Retry-After`, shed at the Caddy edge first, then in mcp-server before business logic.
- Concurrency limiter (semaphore) → `503` when exceeded; worker concurrency tuned to the single core (fewer can be better); per-role connection pools remain the DB-layer bulkhead.
- Backpressure: SQS depth over threshold → `task.create` returns `429 + Retry-After`.
- HTTP rate-limit responses become `429 + Retry-After`.

**Observability (ADR-056)**
- Central JSON-logging config replaces per-entrypoint `basicConfig`. Fields: `ts`, `level`, `service`/role, `event`, `git_sha`, plus `user_id` / `job_id` / `run_id` where present. Ship to Better Stack. Redaction: never log tokens/secrets; log connection ids, not tokens.

**Connection UX (ADR-058)**
- `/connections`: server-rendered, no-framework page behind a WorkOS web session; connect/disconnect per provider (each its own OAuth consent).
- Discoverability: connect URL surfaced on the landing page, in tool/error envelopes (`connect_url`), and in connector instructions. True URL elicitation deferred to v2.
- Caddy path routing extended for `/connections*` and OAuth callback paths.

**Rollout (ADR-059)**
- Operator stdio retained (`MCP_USER_ID` = operator's WorkOS `sub`); public HTTP requires OAuth.
- One-time data migration: `default-user` rows in `jobs` / `job_runs` → `OPERATOR_USER_ID`.
- Simple cutover (no external users yet); the build ships in waves.

**Contracts**
- Tool success/error envelope (CONTEXT §6) extended with an optional `connect_url` on relevant errors.
- New web routes: PRM metadata, `GET /connections`, per-provider OAuth connect + callback.
- `settings.py` gains env knobs (WorkOS, KMS, LLM model + caps, quota caps, Better Stack, `OPERATOR_USER_ID`).

## Testing Decisions

A good test asserts **external behavior**, not implementation details — give an input, assert the observable output/effect, and let internals stay swappable. Tests target the **8 deep modules** (per the agreed module sketch):

1. `auth/token_validation` — fixture JWT + JWKS: valid token → `AuthContext{user_id}`; expired / wrong-audience / bad-signature → reject.
2. `crypto/kms_envelope` — `encrypt → decrypt` round-trip with a **fake KMS**; tampered blob → failure.
3. `connections/store` — `get_fresh_token` returns a valid token; refresh path exercised; missing connection → typed miss; isolation: user A can't read user B's connection. Fake provider + fake KMS.
4. `llm/client` + `llm/actions` — fixed prompt assembled from params; params → prompt mapping; caps enforced (oversized input rejected, `max_output_tokens` applied, budget-over → error). Fake LLM client; no network.
5. `budget/token_accounting` — per-user daily rollover; global monthly ceiling trips.
6. `quotas/containment` — recurring cap, total cap, global ceiling, operator exemption.
7. `overload/health` — `should_shed` thresholds; concurrency limiter rejects past N.
8. `obs/logging` — JSON shape correct; redaction strips token-shaped fields.

Plus a few **integration smoke tests** for the thin wiring: unauthenticated HTTP `/mcp` is rejected; `/connections` renders behind auth; a (fake) OAuth callback stores a connection.

Prior art: `tests/integration/test_ratelimit.py` (model for quota tests), `test_cancel.py`, `test_status.py`, `test_list.py`, `test_http_transport.py`, `test_e2e_inspector_flow.py`, and `tests/unit/test_list_handler.py`. Follow the same fixture/fake style.

## Out of Scope

- True in-chat URL elicitation (deferred v2; link-surfacing covers the gap for now).
- Request prioritization, user tiers, paid plans (all-free this phase).
- Adaptive (latency-driven) concurrency; bulkhead beyond process-role + connection-pool separation; read replica.
- Offloading token storage to WorkOS Pipes / Nango (rejected in ADR-054; revisit only if a free/cheap tier is confirmed).
- Non-Google email senders for public users (SMTP stays operator-only).
- Server-side LLM with free-form / user-supplied prompts (forbidden — abusable open proxy).
- `pg_partman` automation, multi-region, WAF (existing backlog).
- The AWS Fargate path remains a design artifact (ADR-027), not the runtime target.

## Further Notes

- **Observability is a pre-public prerequisite**, promoted from W4-deferred (ADR-024 → ADR-056): the incident runbook's first step is "see what's happening", which the current plain-text `docker logs` cannot serve for a public system.
- **Cost posture**: ~$5 VPS + ~$1 KMS + WorkOS free tier + operator LLM spend bounded by the global monthly ceiling.
- **Suggested wave sequencing** (each shippable): (A) auth resource-server + `user_id` resolver + `default-user` migration; (B) connection store + KMS + `/connections` dashboard + rewrite OAuth-backed actions; (C) LLM actions + budgets; (D) quotas + overload + observability.
- Security property to preserve end-to-end: **no public user's raw secret is ever stored**; only scoped, revocable OAuth tokens (encrypted) and the operator's own env secrets exist.
