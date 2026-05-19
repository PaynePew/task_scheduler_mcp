# PRD: Task Scheduler MCP — W4 Action Sprint

> **Scope**: Vendor-neutral rename (chatgpt_task → task_scheduler_mcp). Five new typed action handlers (slack_post, github_digest, email_send, r2_upload, calendar_digest_ics) with secrets-aware design. Chain data-plane convention for inter-handler data flow. Recent-results MCP resource that surfaces silent successes. Static landing page replacing the bare `/` 404. Postgres-backed rate limiting. L6 Fargate validation tooling + execution. README compact rewrite with i18n. Three deferred-future-direction ADRs. **Not** in scope: server-side LLM (ADR-018-amended documents the deliberate non-decision), atomic plan abstraction (ADR-039 deferred), predicate-based chain (ADR-040 deferred), mcp_call (ADR-038 deferred), ALB OIDC, CloudWatch dashboards, structured JSON logging (W5 commit), localised prompts.
>
> **Deliverable**: A live scheduler at `https://scheduler.paynepew.dev` with a static landing page, 7-handler action registry, daily ops digest workflow (`github_digest → slack_post` weekday 9am Asia/Taipei) running on the VPS, the same workflow validated end-to-end on Fargate via the W3-shipped workflow, and a README compact enough for a 30-second skim with EN/zh-TW switcher.
>
> **Status**: Design 100% locked. Source decisions in Grilling Session #5 (2026-05-19) + ADR-024..031 + new ADRs introduced by this PRD.
>
> **Generated**: 2026-05-19 via `/to-prd` after Grilling Session #5.
>
> **Supersedes**: W3 PRD's "Out of scope for W3, in scope for W4" section. Each inherited candidate is re-evaluated in §D and either shipped, deferred to W5, or permanently rejected.

---

## Problem Statement

After W3 shipped a running scheduler at `scheduler.paynepew.dev` with VPS-first runtime and Fargate-Terraform-as-design-artifact, the project has cleared the L1-L5 acceptance gate. But its **shape as a portfolio piece** still has gaps:

1. **The "Action Sprint" promised in W3's out-of-scope is still empty.** The action registry contains only `echo` (test handler) and `http_call` (generic transport). Without typed handlers for common integrations (Slack, GitHub, email, object storage, calendar), the platform's claim to support "scheduled API workflows" is unproven beyond hand-waving.

2. **W2's chain mechanism has no production use.** It was demonstrated once in W3-VERIFICATION's L4b echo→echo path — a toy. Real chains require **data flow** between steps (slack_post needs github_digest's output), which W2 chain semantics (status-only ordering) don't directly support.

3. **The `${ANTHROPIC_API_KEY}` use case has no path.** `http_call` accepts URLs and headers as plain `action_params`, which are readable via `task.list.v1` — putting an API key in there leaks. Users who want to integrate an LLM (the natural extension under the project's "LLM is the user, not the executor" positioning) currently have no secrets-safe pattern.

4. **Silent successes have no surface.** When a scheduled job completes successfully, the result writes to `JobRun.result` in the DB. Users who closed their LLM client overnight have no way to see "what happened while I was away" without explicitly running `task.list.v1` or `task.status.v1`. The "schedule things to fire when your chat client is closed" tagline has a broken loop: the firing happens, the user never knows.

5. **The course-assignment name `chatgpt_task` is portfolio rot.** It binds the project to one vendor (ChatGPT) when the architecture is vendor-neutral. Senior reviewers will notice the inconsistency between the README's "any MCP client" claim and the repo URL.

6. **`scheduler.paynepew.dev/` returns 404 or unfriendly JSON.** First-visit recruiters who click the demo URL see no orientation. They must infer the GitHub link from the apex domain.

7. **Public demo has trust-only auth with no abuse mitigation.** Any client can create unlimited jobs. A buggy LLM or a casual robot could DOS the scheduler trivially.

8. **The dual-deployment story is unverified.** W3 shipped `validate-fargate.yml` but it has never been pressed. The Fargate Terraform might `terraform plan`-green but fail at `apply` — the design-artifact claim is unbacked.

9. **W4 candidates inherited from W3 PRD (ALB OIDC, CloudWatch dashboards, JSON logging, localised prompts) were enumerated without re-evaluation.** Each needs a current-day judgment: ship, defer, or abandon.

The available solution space for each gap spans: ship typed handlers (resolves #1), evolve chain semantics or sugar (resolves #2), formalize secrets handling (resolves #3), add MCP resource for recent results (resolves #4), rename (resolves #5), ship landing page (resolves #6), implement rate limiting (resolves #7), execute L6 (resolves #8), re-evaluate inherited W4 candidates (resolves #9).

---

## Solution

A twelve-part sprint that ships the Action Sprint promised in W3 and closes every remaining portfolio gap identified above. Each part has explicit ADR documentation; deferred items get future-direction ADRs to document the "deliberately not done" decisions.

1. **Rebrand from `chatgpt_task` to `task_scheduler_mcp`** (ADR-044). Repo / package / Docker image / AWS tags / Better Stack monitor all renamed in one PR sequence. Course-assignment artifact removed; portfolio framing locked.

2. **Five new typed handlers, each with secrets-aware design**:
   - `slack_post` — webhook-based notification sink. Reads `${SLACK_WEBHOOK_URL}` from env. Knows Slack-specific error semantics (429 rate_limited → retry; 404 channel_not_found → DLQ).
   - `github_digest` — GitHub Issues + PRs query. Reads `${GITHUB_TOKEN}` from env. Knows GitHub-specific rate limit semantics.
   - `email_send` — SMTP transactional email. Reads `${SMTP_*}` envs.
   - `r2_upload` — Cloudflare R2 (S3-compatible) object upload. Reads `${R2_*}` envs.
   - `calendar_digest_ics` — Read-only Google Calendar via signed ICS URL. Reads `${GCAL_ICS_URL}` from env.

3. **ADR-032 — Secrets-aware action handlers + `http_call` `${VAR}` substitution.** Per-handler env-only secrets convention. `http_call` gains `${VAR_NAME}` substitution in any string field, gated by a whitelist (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY`, `SLACK_WEBHOOK_URL`, `GITHUB_TOKEN`, operator-extensible). Pre-flight detection of literal secrets in `task.create.v1` (best-effort pattern match) rejects accidental literal keys with a fixable error.

4. **ADR-033 — Inter-handler data flow via `JobRun.result` + `from_run_id` convention.** `JobRun.result` (existing `Text | None` column) becomes the documented inter-handler data plane. Handlers MAY accept `from_run_id: int | None` in their `params_model`; if present, they read upstream's `result` (parsed as JSON), treating it as primary input. ChainWatcher remains unchanged (status-only flips). Daily digest pattern (`trigger_on_status=ANY` + slack_post handler internal ok-path/error-path branch) standardized as the recommended pattern for sink-to-human chains.

5. **ADR-037 — `tasks://recent-results` MCP resource.** New static resource that, when read by an MCP client, returns a summary of the last 24h of completed / failed / cancelled `JobRun`s for the calling `user_id`. Closes the broken loop identified in problem #4: LLM clients on connect surface the resource and can proactively brief the user on overnight activity.

6. **ADR-018-amended — W4 reconsidered server-side LLM, decided to remain LLM-agnostic.** Documents the deliberate non-decision: server-side `llm_*` actions would dilute the differentiator against ChatGPT Tasks / Zapier / Codex. Users wanting LLM in their workflows compose `http_call` + `${VAR}` against any LLM vendor.

7. **ADR-038, 039, 040 — Three future-direction ADRs**: `mcp_call` (worker as MCP client), `Plan` (atomic multi-step abstraction), predicate-based chain (conditional workflow). All documented with v2 schema sketches + reasons for W4 deferral.

8. **ADR-041 — Static landing page at `/`** served by Caddy via path-based routing. mcp-server claims `/healthz` and `/mcp*`; everything else falls through to a single `index.html`. ADR-worthy because all future endpoints (`/admin`, `/metrics`, etc.) must respect this convention.

9. **ADR-042 — Postgres-backed rate limiting.** Two query-based windows: per-day (default 1000 jobs/user/24h) + per-minute burst (default 10/minute). Postgres chosen over Redis to avoid new dependency; multi-replica state shared via DB. `task.create.v1` returns structured error with `retry_after_seconds`. Limit values configurable via env.

10. **L6 Fargate validation tooling + execution.** Five additions to `validate-fargate.yml`: `dry_mode` input (plan-only, $0), three post-apply sanity gates (ECS converged, ALB targets healthy, RDS available), reuse of `bin/w3-smoke.py` for L4a on Fargate, resource-tag orphan check post-destroy, concurrency lock. Two executions: dry run (~$0.10) and recording run with `duration_minutes=180` (~$1-2).

11. **README compact rewrite + i18n switcher.** Target ~150 lines (60% smaller). English primary at `README.md`, Traditional Chinese at `README.zh-TW.md`. Three architecture diagrams: D1 system overview (Mermaid), D2 dual deployment (Excalidraw), D3 event flow (Mermaid, lives in ADR-009 not README). One 30-60s hero GIF, four screenshots, terminology fixes ("webhook" → "outbound API calls"), persona statement, and honest MCP client compatibility list (explicitly noting ChatGPT does NOT support MCP). Roadmap acceptance gate table preserved.

12. **Daily ops digest go-live.** Real recurring workflow on the production VPS: `github_digest` (weekday 9am Asia/Taipei, repo=PaynePew/task_scheduler_mcp, labels=[needs-triage, needs-info, ready-for-agent]) → `slack_post` (chained with `trigger_on_status=ANY`, `from_run_id` reading upstream digest, internal ok/error formatting). A second workflow: `calendar_digest_ics` (weekday 8am, user's own Google Calendar ICS URL) → `slack_post` (interview reminders). Both dogfood the platform.

Out of scope under this PRD, explicitly with their resolution: ALB OIDC (ADR-024 already documents deferred), CloudWatch dashboards (replaced by L6 evidence artifacts), JSON-only logging (insufficient; W5 ships full structured logging with correlation IDs, ADR-043), localised UI/prompts (covered by README i18n + landing page), `http_health_check` (Better Stack covers the use case; insufficient ADR-density to justify; cut entirely), 3-minute edited demo video (L7 downgraded to GIF + screenshots + diagrams; recording-grade video deferred to "spare time after W4").

---

## User Stories

### As the project owner positioning a portfolio piece

1. As the project owner, I want the project name to be vendor-neutral, so that the repo URL doesn't contradict the README's "any MCP client" claim.
2. As the project owner, I want a persona statement on the README, so that visitors can self-select in or out within 10 seconds.
3. As the project owner, I want the README under 200 lines, so that 30-second skim readers actually finish it.
4. As the project owner, I want the README in both English and Traditional Chinese, so that Taiwanese recruiters and international engineers each face zero language friction.
5. As the project owner, I want three architecture diagrams (overview, dual-deployment, event-flow), so that visual learners can grok the system without reading prose.
6. As the project owner, I want a 30-60s hero GIF in the README, so that the happy-path UX is communicated without prose.
7. As the project owner, I want screenshots of Claude Desktop tools / MCP Inspector / Better Stack status page, so that the "this is real" signal is visually verifiable.
8. As the project owner, I want a clear terminology distinction between "webhook" (inbound) and "outbound API calls" (what this MCP does), so that interview readers don't pattern-match wrong.
9. As the project owner, I want the README to explicitly disclose that ChatGPT does NOT speak MCP, so that no recruiter is misled into a false promise.
10. As the project owner, I want all design decisions (including deliberate non-decisions like ADR-018-amended) documented as ADRs, so that any reviewer can audit the judgment trail.

### As an LLM client user creating scheduled workflows

11. As an LLM client user, I want to schedule `slack_post` jobs without writing the webhook URL into `action_params`, so that my Slack webhook isn't readable via `task.list.v1`.
12. As an LLM client user, I want a `github_digest` action that takes my repo + labels and returns a structured payload, so that I don't have to hand-craft GitHub API URLs.
13. As an LLM client user, I want an `email_send` action so that workflows can deliver via email, not only Slack.
14. As an LLM client user, I want an `r2_upload` action so that workflows can persist artifacts to object storage.
15. As an LLM client user, I want a `calendar_digest_ics` action so that workflows can react to my calendar without requiring OAuth setup.
16. As an LLM client user, I want my LLM to use `http_call` with `${ANTHROPIC_API_KEY}` substitution, so that I can plug in any LLM provider without committing my key to action_params.
17. As an LLM client user, I want the system to reject `task.create.v1` calls containing literal-looking API keys (e.g., `sk-ant-…` in headers), so that I get an immediate fixable error instead of leaking the key into the DB.
18. As an LLM client user, I want the daily digest workflow to use `trigger_on_status=ANY` with `slack_post` formatting upstream failures into a Slack alert, so that I'm notified when the digest fetch fails — silent failure is unacceptable for a 9am brief.
19. As an LLM client user opening Claude Desktop on Monday morning, I want Claude to proactively tell me what jobs ran over the weekend, so that I don't have to remember to ask.
20. As an LLM client user, I want `task.create.v1` to reject me when I exceed 1000 jobs/24h or 10 jobs/minute, so that runaway loops fail fast rather than fill the DB.

### As a chain workflow designer

21. As a workflow designer, I want `slack_post` to accept `from_run_id` and read upstream `JobRun.result`, so that I can chain `github_digest → slack_post` without writing a custom wrapper.
22. As a workflow designer, I want the `from_run_id` convention to be a **convention**, not a forced Protocol or mixin, so that handlers can opt in without architectural ceremony.
23. As a workflow designer, I want ADR-033 to spell out the recommended pattern (`trigger_on_status=ANY` + internal handler branching) for sink-to-human chains, so that I have a copy-paste template.
24. As a workflow designer, I want `email_send` to also support `from_run_id`, so that the same chain pattern works for email sinks as for Slack.
25. As a workflow designer, I want `JobRun.result` documented as the inter-handler data plane in CONTEXT.md, so that future handler authors know to write parseable JSON.

### As a first-visit recruiter clicking the demo URL

26. As a first-visit recruiter, I want `scheduler.paynepew.dev/` to return a friendly landing page, so that I don't see a 404 or raw JSON.
27. As a first-visit recruiter, I want the landing page to link prominently to the GitHub source, so that I can drill in within one click.
28. As a first-visit recruiter, I want the landing page to display the Better Stack status badge, so that the "live and healthy" signal is immediate.
29. As a first-visit recruiter, I want the landing page mobile-responsive, so that clicking from LinkedIn on my phone doesn't break the layout.

### As an SRE/DevOps reviewer drilling into Fargate validation

30. As an SRE reviewer, I want `validate-fargate.yml` to support a `dry_mode` input that runs `terraform plan` only, so that workflow logic can be sanity-checked without burning AWS cost.
31. As an SRE reviewer, I want post-apply assertions on ECS service `runningCount == desiredCount`, ALB target health, and RDS `available` status, so that downstream smoke failures are diagnosed at the right layer.
32. As an SRE reviewer, I want the L6 workflow to reuse the same `bin/w3-smoke.py` that validates the VPS, so that "platform parity" is mechanically true, not asserted.
33. As an SRE reviewer, I want a Resource Groups Tagging API check after destroy, so that I can prove no orphaned resources accrue cost.
34. As an SRE reviewer, I want the workflow to have a `concurrency:` lock, so that an accidental double-trigger doesn't spin up two stacks racing for state lock.
35. As an SRE reviewer, I want the dry run + recording run to both execute within W4 and produce evidence artifacts retained for 90 days, so that the "Fargate Terraform is real" claim has receipts.

### As an interviewer / senior reviewer drilling into ADRs

36. As an interviewer, I want ADR-018-amended to record that W4 reconsidered server-side LLM and deliberately stayed LLM-agnostic, so that I can read the "why not Codex/Zapier/ChatGPT Tasks" reasoning before asking.
37. As an interviewer, I want ADR-038/039/040 to exist as Deferred-status decisions covering `mcp_call`, plan abstraction, and predicate-based chain, so that I can evaluate the candidate's judgment on what NOT to ship.
38. As an interviewer, I want ADR-032 to explain why secrets are env-only with whitelisted `${VAR}` substitution, including the literal-detection escape hatch and its limitations, so that I can probe the security model.
39. As an interviewer, I want ADR-042 to explain Postgres-vs-Redis for rate limiting and document the conditions under which Redis adoption becomes necessary, so that I can probe the scaling judgment.
40. As an interviewer, I want each new ADR to follow the same Context → Decision → Alternatives → Consequences structure as ADR-001..031, so that the cohort reads consistently.

### As a self-hoster wanting to extend with LLM

41. As a self-hoster, I want to add `ANTHROPIC_API_KEY` (or any whitelisted env var) to my VPS `.env`, restart worker, and immediately be able to schedule LLM-calling workflows via `http_call` + `${VAR}`, so that BYO-LLM has zero friction.
42. As a self-hoster, I want the `${VAR}` whitelist to be operator-extensible via `ALLOWED_TEMPLATE_VARS`, so that I can add my own API keys without forking the codebase.
43. As a self-hoster, I want non-whitelisted env vars (DB password, SSH key) to be unreadable via `${VAR}` substitution, so that a malicious `action_params` can't exfiltrate secrets.

### As an operator running the public demo

44. As an operator, I want rate limiting in place before exposing the demo URL publicly, so that a casual robot doesn't fill my DB.
45. As an operator, I want the rate limit thresholds tunable via env var, so that I can dial back if a real user needs higher headroom.
46. As an operator, I want the ADR to acknowledge that in-process burst counter doesn't share state across `mcp-server` replicas, with a documented upgrade path, so that I can plan for scale without surprise.
47. As an operator, I want the landing page CSS / HTML in the repo (version-controlled), so that updates go through PR review.
48. As an operator, I want `validate-fargate.yml` runs to stay under $5 each via duration caps + AWS Budgets, so that I can experiment without bill anxiety.

### As an MCP Inspector / Claude Desktop user verifying the install

49. As a verifier, I want `tools/list` to show 5 tools (unchanged from W2), so that existing W2-VERIFICATION steps still pass.
50. As a verifier, I want `resources/list` to now show 4 entries (existing 3 + new `tasks://recent-results`), so that the briefing surface is discoverable.
51. As a verifier, I want `tasks://recent-results` to return the user's last 24h of `JobRun`s with status, action name, and timestamp, so that I can audit overnight activity.
52. As a verifier, I want `task.list_actions.v1` to return 7 actions (echo, http_call, slack_post, github_digest, email_send, r2_upload, calendar_digest_ics), so that the registry growth is observable.

---

## Implementation Decisions

### D1. Project rename (ADR-044)

`chatgpt_task` → `task_scheduler_mcp` across:

- GitHub repo (auto-redirect old URL)
- Python package name in `pyproject.toml`
- Docker image: `ghcr.io/paynepew/task_scheduler_mcp`
- AWS resource tags: `Project=task-scheduler-mcp`
- Better Stack monitor name
- All ADR / PRD / runbook text references

Domain `scheduler.paynepew.dev` not affected (already neutral). Rename PR executed as the first slice before any other work. Old image tags retained in `ghcr.io` storage indefinitely (free for public packages) for backward compat with anyone who happened to pull `chatgpt_task` historically.

### D2. Secrets resolver as deep module (ADR-032)

Extract `app/secrets/resolver.py` and `app/secrets/literal_detection.py` as pure-function deep modules.

**Resolver interface (decision; not code):**

- Input: any value (str, dict, list, recursively), env mapping, whitelist set
- Behavior: replace `${VAR}` substrings in any string field with `env[VAR]` if `VAR ∈ whitelist`; raise `SecretResolutionError` (retryable=False) if VAR not in whitelist or not in env
- Output: structurally identical value with substitutions applied

**Whitelist defaults (env var `ALLOWED_TEMPLATE_VARS`)**: `ANTHROPIC_API_KEY,OPENAI_API_KEY,GOOGLE_API_KEY,SLACK_WEBHOOK_URL,GITHUB_TOKEN`. Operator-extensible. DB / SSH / monitor tokens NEVER in whitelist.

**Literal detection interface**: regex match against known secret prefixes (`sk-ant-`, `sk-`, `xoxb-`, `ghp_`, `glpat-`, `AIza`, etc.). Called by `task.create.v1` on incoming `action_params` recursively. Match → reject with `USER_INPUT` error code, `expected` field suggesting `${VAR_NAME}` form.

`http_call`'s `execute()` calls resolver first, before any HTTP I/O.

### D3. Chain data plane via `JobRun.result` (ADR-033)

`JobRun.result` (existing `Text | None` column, no schema change) becomes the documented inter-handler data plane:

- Upstream handlers writing structured output SHOULD serialize to JSON string and write to `result`
- Downstream handlers accepting `from_run_id: int | None` in `params_model` read upstream's result and treat it as primary input
- ChainWatcher (W2) unchanged — still only flips status, never touches `result`

Extract `app/chain/upstream_reader.py` as deep module:

- `async read_upstream(run_id, session) -> UpstreamPayload`
- UpstreamPayload variants: `Ok(parsed_json)`, `UpstreamError(error_msg)`, `NoResult`, `InvalidJson`
- Handler dispatches on variant

**Recommended chain pattern (Design B, daily digest standard)**: `trigger_on_status=ANY` + downstream handler's internal branching on UpstreamPayload variant. Sink-to-human chains use this. Silent-skip chains (chained alarms, multi-stage pipelines) keep `SUCCEEDED`. Documented in ADR-033 commentary.

**Anti-pattern explicitly named in ADR**: do NOT create handlers like `slack_post_from_github_digest`. Specialization is via params (`from_run_id` value), not via class names.

### D4. Five new typed handlers, each invoking secrets resolver

All handlers follow the same skeleton: `params_model` (Pydantic), `execute(run, params)` calls `secrets_resolver.resolve(...)` first, then handler-specific I/O.

**`slack_post`**:

- Params: `channel: str`, `message: str | None`, `from_run_id: int | None`, `template: str | None`
- Secret: `${SLACK_WEBHOOK_URL}` via env
- Error class: 429 → retry; 4xx auth/channel errors → DLQ; 5xx → retry; timeout → retry
- Template enum (W4 initial): `"raw"`, `"digest_v1"`, `"interview_brief"` — each handles `from_run_id` branching

**`github_digest`**:

- Params: `repo: str`, `labels: list[str]`, `pr_stale_days: int = 3`
- Secret: `${GITHUB_TOKEN}` via env
- Output: structured JSON `{repo, queried_at, labels: {label: [issues...]}, prs: {open, stuck}}` to `JobRun.result`
- Error class: 401 → DLQ; 403 rate-limited → retry; 403 forbidden → DLQ; 404 → DLQ; 422 → DLQ; 5xx → retry
- Does NOT honor `x-ratelimit-reset` — relies on SQS visibility timeout + retry count (documented trade-off in ADR-013 commentary)

**`email_send`**:

- Params: `to: list[EmailStr]`, `subject: str`, `body: str | None`, `from_run_id: int | None`, `template: str | None`
- Secrets: `${SMTP_HOST}`, `${SMTP_PORT}`, `${SMTP_USER}`, `${SMTP_PASSWORD}`, `${EMAIL_FROM}`
- Transport: `aiosmtplib` (async)
- ADR-045: SMTP-vs-API-service trade-off; bounce handling (don't auto-retry 5.x.x permanent failures)

**`r2_upload`**:

- Params: `bucket_path: str`, `content: str | None`, `from_run_id: int | None`, `content_type: str = "application/octet-stream"`
- Secrets: `${R2_ACCOUNT_ID}`, `${R2_ACCESS_KEY_ID}`, `${R2_SECRET_ACCESS_KEY}`, `${R2_BUCKET}`
- Transport: `boto3` with R2 endpoint URL (S3-compatible)
- Idempotency: file path including hash of content; same content → same path → no-op (let R2 dedupe)
- ADR-046: R2 over S3 (cost + egress + existing W3 backup integration); multipart threshold; promotion of W3 nightly cron from shell script to typed action

**`calendar_digest_ics`**:

- Params: `ics_url: str` (typically `${GCAL_ICS_URL}`), `date_range_days: int = 1`, `title_contains: str | None`
- Secret: `${GCAL_ICS_URL}` (URL contains bearer token)
- Transport: `httpx` GET, then parse via extracted `app/ics/parser.py` (deep module wrapping `icalendar` library)
- Output: structured JSON list of events to `JobRun.result`
- ADR-048: ICS-vs-OAuth trade-off (4× effort for 1.1× capability); URL-as-token security model trade-off

### D5. `tasks://recent-results` MCP resource (ADR-037)

New static MCP resource. URI: `tasks://recent-results`. Reads scoped to the calling `user_id`.

Returns last 24h of completed / failed / cancelled `JobRun`s as MCP resource content (decision shape; not code):

```
{
  "queried_at": "<iso>",
  "user_id": "<uid>",
  "window_hours": 24,
  "runs": [
    {"job_id": N, "run_id": N, "action": "...", "status": "...",
     "start_at": "...", "finish_at": "...", "error_excerpt": "..."},
    ...
  ]
}
```

Bounded to most-recent 50 runs to avoid context bloat. New MCP resource module: `app/mcp/resources/recent_results.py`.

Briefing pattern (documented in ADR-037 commentary): LLM clients on connect surface the resource → can proactively brief on overnight activity. Closes the silent-success loop from problem #4.

### D6. Static landing page + Caddy path routing (ADR-041)

**Caddyfile change**:

- Before: `reverse_proxy localhost:8080` (catch-all)
- After: `handle /healthz { reverse_proxy ... }; handle /mcp* { reverse_proxy ... }; handle / { root * /var/www; file_server }`

**Landing page (`infra/vps/static/index.html` + `style.css`)**:

- Single-page, no JS
- Hero section: project name + tagline (English; matching README)
- Hero asset: same GIF as README hero (single source of truth)
- Three buttons: GitHub source, README, Status page
- Footer with disclaimer matching README ("public demo; trust-only; self-host for serious use")
- Mobile-responsive via viewport meta + flexbox

**Path namespace decision (ADR-041 consequence)**: mcp-server now formally claims `/healthz` and `/mcp*`. Any future endpoint (e.g., `/admin`, `/metrics`) must explicitly register a path matcher.

### D7. Postgres-backed rate limiting (ADR-042)

Extract `app/ratelimit/checker.py` as deep module.

**Interface**: `async check_rate_limit(user_id, session, limits: RateLimits) -> RateLimitDecision`

- `RateLimits`: `daily: int`, `burst: int` (per-minute)
- `RateLimitDecision`: `Allow` | `Reject(reason, retry_after_seconds)`

**Implementation**: two SELECT COUNT(*) against `jobs` table filtered by `user_id` and `created_at` window. Reuses existing index on `(user_id, created_at)` if present; otherwise new partial index added in migration.

**Limits (env-configurable)**:

- `RATE_LIMIT_DAILY` (default 1000)
- `RATE_LIMIT_BURST_PER_MINUTE` (default 10)

**Integration**: `task.create.v1` handler calls `check_rate_limit` first. Reject → returns MCP error envelope with `code: "USER_INPUT"`, `message: "Rate limit exceeded (...)"`, includes `retry_after_seconds` in `expected` field.

**Documented limitations (ADR-042 Consequences)**:

- In-process per-minute counter not shared across `mcp-server` replicas → effective burst limit = `burst × replica_count`
- Restart resets counter → no historical state
- Postgres COUNT performance: acceptable while `jobs` table < 1M rows; beyond that, consider Redis or DB-aggregated counters
- Trigger condition for Redis upgrade: sustained > 10 req/s OR `jobs` table > 1M rows OR replica_count > 4

### D8. Three deferred future-direction ADRs

**ADR-038 — `mcp_call` action (deferred to W5+)**: Worker as MCP client; orchestrate other MCP servers (GitHub MCP, Slack MCP, Notion MCP). v2 design sketch included: MCP client SDK integration, per-target connection lifecycle (open-close per call vs pooled), tool discovery cache invalidation, auth model per remote MCP. Deferred because W4 already packed; current `http_call` covers most use cases via raw JSON-RPC.

**ADR-039 — `Plan` as first-class entity (deferred to v2)**: Multi-step plans need atomic create/cancel/status. v2 schema sketch: new `plans` table, `plan_id` FK on `jobs`, new tools `plan.create.v1` / `plan.cancel.v1` / `plan.status.v1`. Deferred because schema migration is significant; W4 chain + `from_run_id` convention covers ~80% of two-step cases.

**ADR-040 — Predicate-based chain (deferred to v2)**: Conditional workflows ("if upstream.result satisfies X then ..."). Three evaluator candidates: jq syntax, CEL (Google), Lua (sandboxed). Trade-off: expressiveness vs sandbox security. Deferred; current workaround is "push the condition into the upstream handler and chain on FAILED".

### D9. ADR-018-amended

Revisit ADR-018 (no server-side LLM in W2) with W4-era information. Decision unchanged: stay LLM-agnostic. New context: ChatGPT Tasks now exists; Codex schedule exists; LangChain/LangGraph dominate LLM workflow framing. Differentiator preserved: **the LLM is the user, not the executor**. The MCP is the persistent backend that fires when chat clients are closed. Users wanting LLM call it via `http_call` + whitelisted `${VAR}`.

### D10. L6 Fargate validation tooling (5 additions to `validate-fargate.yml`)

| Addition | Effect |
|---|---|
| `dry_mode: bool` input | When true, skip apply/smoke/destroy; only run init + plan + capture plan as artifact. Zero AWS cost. |
| Three post-apply sanity gates | `aws ecs describe-services` (runningCount==desiredCount), `aws elbv2 describe-target-health` (all healthy), `aws rds describe-db-instances` (status=available). Each fail-fast with explicit error message. |
| Smoke step reusing `bin/w3-smoke.py` | Invoke with `--url=https://${ALB_DNS}` to run L4a + L4b on Fargate stack. Same script that validates VPS daily. |
| Resource Groups Tagging API orphan check | After destroy + 60s grace, query `aws resourcegroupstaggingapi get-resources --tag-filters Key=Project,Values=task-scheduler-mcp`. Length > 0 → fail with list of remaining resources. |
| Concurrency lock | `concurrency: { group: validate-fargate, cancel-in-progress: false }` in workflow header. |

### D11. L6 execution plan

- **Dry run** (W4 week 2-3, mid-sprint): `dry_mode=true`, `duration_minutes=15`. Cost: ~$0.10. Catches workflow bugs before recording day.
- **Recording run** (W4 week 3, recording day): `dry_mode=false`, `duration_minutes=180`. Cost: ~$1-2. Used for L7 alt-artifacts (screenshots, evidence capture). Includes L4a recurring proof on Fargate stack (5-min wait).

`docs/runbooks/pre-fargate-validation-checklist.md` updated with: AWS Budgets confirmation, IAM key validity, prior-run cleanup verification, expected vs actual cost reconciliation.

### D12. README compact rewrite + i18n

**File structure**:

- `README.md` — English, primary, GitHub default render
- `README.zh-TW.md` — Traditional Chinese, equal-content translation
- Top of each file: `**🌐 English** | [繁體中文](README.zh-TW.md)` (and reverse on zh-TW)

**Section structure (target ~150 lines)**:

1. Title + tagline + i18n switcher + status badges
2. Hero GIF
3. Persona statement
4. Quick architecture (Mermaid D1 + ~5 line paragraph)
5. How to use (3 paths)
6. Why HTTP not stdio
7. Deployment architecture (Excalidraw D2 + table)
8. Roadmap (acceptance gate table, all green after W4 ships)
9. Design decisions (ADR list, ~24 entries, Deferred ones marked with status)
10. MCP surface (7 tools / 4 resources / 2 prompts; recurring/chain/cancel mentioned as features here, not as "bonus")
11. Local development + Verify (compressed; link to W2-VERIFICATION.md)

**Cut sections** (per Grilling Session #5): Bonus Challenges, Cost Transparency, Future Direction. MCP client compatibility folded into MCP surface section.

**Visual artifacts**:

- D1 system overview — Mermaid `flowchart`, inline in README
- D2 dual deployment — Excalidraw PNG + source `.excalidraw` in `docs/diagrams/`
- D3 event flow — Mermaid `sequenceDiagram`, inline in ADR-009 (not README)
- 1 hero GIF — `docs/diagrams/hero.gif`, 1280×720, < 5MB, ~30-60s
- 4 screenshots — Claude Desktop tools, MCP Inspector, Better Stack status, `task.list.v1` response

**Terminology fixes**:

- "webhook" used for inbound only; "outbound API calls" / "action targets" for the worker's outbound HTTP
- Persona statement: "Built for: developers who run their own webhooks/APIs and want to schedule them via natural-language LLM chat, with auditable persistence beyond chat sessions."
- Client compatibility: explicit list (Claude Desktop, Cursor, Claude in Chrome, MCP Inspector); explicit non-support note for ChatGPT (Custom GPT Actions ≠ MCP)

### D13. Daily ops digest go-live

Two real recurring workflows created on production VPS:

**Workflow 1 — GitHub triage digest**:

```
Job A: github_digest, cron="0 9 * * 1-5" Asia/Taipei
       params: repo="PaynePew/task_scheduler_mcp", labels=[needs-triage, needs-info, ready-for-agent]
Job B: slack_post chained on A, trigger_on_status=ANY, from_run_id=<A>, template="digest_v1"
```

**Workflow 2 — Calendar / interview reminder**:

```
Job C: calendar_digest_ics, cron="0 8 * * 1-5" Asia/Taipei
       params: ics_url="${GCAL_ICS_URL}", title_contains="interview"
Job D: slack_post chained on C, trigger_on_status=ANY, from_run_id=<C>, template="interview_brief"
```

Both jobs created manually via MCP Inspector against the live VPS after handler ship. G3 closes when Slack channel shows ≥ 5 consecutive workday brief messages.

### D14. Module surface

| Module | Status | Purpose |
|---|---|---|
| `app/secrets/resolver.py` | New (deep) | `${VAR}` substitution + whitelist enforcement |
| `app/secrets/literal_detection.py` | New (deep) | Best-effort pattern match for literal secrets |
| `app/ics/parser.py` | New (deep) | iCalendar parsing wrapper |
| `app/chain/upstream_reader.py` | New (deep) | `from_run_id` convention reader |
| `app/ratelimit/checker.py` | New (deep) | Postgres-backed rate limit query |
| `app/actions/slack_post.py` | New (handler) | Slack webhook sink |
| `app/actions/github_digest.py` | New (handler) | GitHub Issues/PRs query |
| `app/actions/email_send.py` | New (handler) | SMTP email |
| `app/actions/r2_upload.py` | New (handler) | R2/S3 object upload |
| `app/actions/calendar_digest_ics.py` | New (handler) | Google Calendar ICS read |
| `app/actions/http_call.py` | Modified | Add `${VAR}` substitution at entry |
| `app/actions/registry.py` | Modified | Register 5 new handlers |
| `app/mcp/resources/recent_results.py` | New | `tasks://recent-results` MCP resource |
| `app/mcp/server.py` | Modified | Wire rate-limit check before task.create.v1; register recent-results resource |
| `app/mcp/handlers/` (create handler) | Modified | Invoke literal-detection + rate-limit pre-flight |
| `infra/vps/Caddyfile` | Modified | Path-based routing |
| `infra/vps/static/index.html` | New | Landing page |
| `infra/vps/static/style.css` | New | Landing page styles |
| `infra/vps/docker-compose.yml` | Modified | Mount `/var/www` for Caddy + new env vars |
| `.github/workflows/validate-fargate.yml` | Modified | 5 tooling additions |
| `pyproject.toml` | Modified | Deps: `aiosmtplib`, `boto3`, `icalendar`; package rename |
| `README.md` | Rewrite | Compact + i18n switcher + visual artifacts |
| `README.zh-TW.md` | New | Traditional Chinese version |
| `docs/diagrams/` | New | Mermaid + Excalidraw sources + hero GIF + screenshots |
| `docs/adr/ADR-018-amended.md` | New | W4 reconsidered LLM, decided stay agnostic |
| `docs/adr/ADR-032 / 033 / 037 / 041 / 042 / 044 / 045 / 046 / 048.md` | New | Per-decision ADRs (impl-bearing) |
| `docs/adr/ADR-038 / 039 / 040.md` | New | Future-direction ADRs (doc-only, Deferred status) |
| `docs/runbooks/pre-fargate-validation-checklist.md` | Modified | Updated for W4 execution |
| `docs/W4-VERIFICATION.md` | New | Manual click-through for daily digest + landing page + rate limit smoke |
| `docs/PRD/action-sprint-w4.md` | This file | Sprint spec |

---

## Testing Decisions

### What makes a good test (extending W2/W3)

- **Test handler behavior, not handler structure**: A test that mocks `httpx.AsyncClient` and asserts "execute() returns ActionResult(ok=True)" tests behavior. A test that asserts "handler has a method called `_build_payload`" tests structure.
- **Test deep modules in isolation, never through the handler**: `secrets_resolver.resolve()` gets its own test file. Handler tests assume resolver works.
- **Don't mock Postgres in integration tests** (per W2 carry-over): integration tests use the Compose-launched Postgres. Mocks at unit layer, real DB at integration.
- **Mock external HTTP at integration layer**: Slack webhook, GitHub API, R2 endpoint, SMTP, calendar ICS fetch all mocked. Real-external smoke tests are MANUAL only (`@pytest.mark.smoke`, never in CI).
- **Test error classification**: Every handler has an error class table (status code → retryable). Tests cover each major bucket (success, retryable failure, non-retryable failure, network timeout).
- **Test the `from_run_id` convention** at the upstream_reader layer: each variant (`Ok`, `UpstreamError`, `NoResult`, `InvalidJson`) gets a unit test. Handler tests assume reader returns correct variant.
- **Test rate limit boundaries**: integration test that creates 1000 jobs successfully then asserts 1001 returns rate-limit error. Don't rely on unit test alone.

### Test surface by module

| Module | Test type | What's covered |
|---|---|---|
| `app/secrets/resolver` | Unit | String/dict/list recursion; whitelist enforcement; unknown var; missing env; nested substitution |
| `app/secrets/literal_detection` | Unit | Each known prefix; false-positive avoidance; nested location detection |
| `app/ics/parser` | Unit (fixture ICS files) | Single-event, multi-event, recurring, all-day, malformed, empty calendar, timezone handling |
| `app/chain/upstream_reader` | Unit + Integration | Each variant (Ok/Error/NoResult/InvalidJson); real DB lookup; missing run_id |
| `app/ratelimit/checker` | Unit + Integration | Under-limit allow; over-limit reject; window boundary; multiple users isolated; real DB COUNT |
| `app/actions/slack_post` | Unit + Integration (mock webhook) | Happy POST; 429 retry; 404 DLQ; 5xx retry; from_run_id branch (ok-path); from_run_id branch (error-path); template variants |
| `app/actions/github_digest` | Unit + Integration (mock GitHub) | Happy query; 401 DLQ; 403 rate-limited retry; 404 DLQ; empty result; label filtering; JSON output schema |
| `app/actions/email_send` | Unit + Integration (aiosmtpd mock SMTP) | Single recipient; multi recipient; bounce response; auth failure; TLS handshake failure; from_run_id usage |
| `app/actions/r2_upload` | Unit + Integration (moto or LocalStack S3) | Small upload; large upload (multipart); idempotency (same content same path); 4xx auth failure; 5xx retry |
| `app/actions/calendar_digest_ics` | Unit + Integration (httpx mock returning fixture ICS) | Single event found; multi-day range; title_contains filter; 404 ICS URL; invalid ICS content; empty calendar |
| `app/actions/http_call` (modified) | Unit (regression) | Existing behavior preserved; new `${VAR}` substitution; non-whitelisted VAR rejection |
| `app/mcp/resources/recent_results` | Unit + Integration | Empty result (no recent runs); 50-row cap; user_id scoping; status filter; ordering by finish_at desc |
| `task.create.v1` (modified) | Integration | Pre-flight literal detection rejects `sk-ant-`; rate limit rejection at 1001st create; rate limit reset after window |
| `validate-fargate.yml` additions | Manual (workflow IS the test) | Dry mode skips apply; sanity gates fail-fast when ECS not converged; orphan check catches synthetic leftover; concurrency lock blocks second trigger |
| Landing page | Manual smoke | `curl https://scheduler.paynepew.dev/` returns 200 + HTML with expected hero |
| Daily digest workflow | Live observation | Real Slack channel shows ≥ 5 consecutive workday messages; failure-path message appears when manually inducing failure |

### Coverage target

Application coverage from W1+W2 (80%+) maintained. New deep modules expected to hit 95%+ (small surface, fully testable). Handlers expected 80%+ (happy + main error branches). Rate limit + recent-results resource included in W4 integration test suite. Live workflow validation is observation, not pytest.

### Prior art

- W1+W2 `tests/integration/test_e2e_inspector_flow.py` — in-process MCP testing pattern; W4 adds 3 new steps (slack_post smoke, github_digest smoke, recent_results resource read)
- W3 `bin/w3-smoke.py` — extended (not modified) to be the L4a/L4b runner for Fargate via `--url`
- W2 chain validation tests (`tests/integration/test_chain_watcher.py`) — extended with `from_run_id` data-flow scenarios

---

## Out of Scope

These belong to W5+ or are permanently rejected:

### Out of scope for W4, in scope for W5

- **Structured logging with correlation IDs (ADR-043)**: full structured logging spec — `correlation_id` propagation from `task.create.v1` through worker / RunEvent / chain, structured fields (user_id, job_id, run_id) always present, consistent log levels per operation, `LOG_FORMAT=json|plain` env var. W5 must-do per grilling decision.
- **`http_health_check` assertion-style handler**: assertion DSL on response status/body/latency. Cut from W4 (Better Stack covers operational use case); W5 nice-to-have.
- **Blog post**: deferred until W4 ships. Outline includes VPS pivot cost story, three-principle alignment (start simple → start deterministic → loosen up gradually), "why I didn't add LLM" narrative, silent-failure UX gap + ADR-037 solution.

### Out of scope for W4, deferred to v2 (future-direction ADRs)

- **`mcp_call` action — worker as MCP client (ADR-038)**: orchestrate other MCP servers (GitHub MCP, Slack MCP, Notion MCP). Documented as future direction; current `http_call` covers via raw JSON-RPC if needed.
- **`Plan` as first-class entity (ADR-039)**: atomic multi-step plan create/cancel/status. v2 schema designed; deferred to avoid schema migration during sprint.
- **Predicate-based chain (ADR-040)**: conditional workflows beyond status-only. v2 evaluator candidates documented (jq / CEL / Lua); deferred.

### Permanently out of scope (in W4 and beyond)

- **Server-side LLM actions** (`llm_summarize`, `llm_chat`): ADR-018-amended documents the deliberate decision to remain LLM-agnostic. Users compose `http_call` + `${VAR}` against any LLM vendor.
- **ALB OIDC integration**: ADR-024 already documents deferred; no third-party users to justify. README persona statement points to ADR-024 for the auth model.
- **CloudWatch dashboards**: replaced by L6 evidence artifacts (ECS/RDS/ALB describe outputs); without production traffic, dashboards are theater.
- **Localised UI/prompts**: README i18n (W4) is sufficient. MCP prompt template translation premature.
- **`ssh_command` action**: arbitrary command execution + trust-only public demo = unacceptable attack surface.
- **`kubernetes_apply` / `terraform_apply` actions**: stack doesn't use k8s; terraform action is recursive (we use Terraform to deploy ourselves).
- **`sql_query` action**: opens security can of worms; read-only enforcement requires SQL parser; deferred unless real user demand emerges.

---

## Further Notes

### Companion documents (must-read for implementers)

- **Grilling Session #5 conversation** (this PRD's source) — captured in the conversation that produced this file
- **`docs/PRD/prototype-w1.md`**, **`docs/PRD/bonus-w2.md`**, **`docs/PRD/deploy-w3.md`** — the surfaces this W4 sprint extends
- **`CONTEXT.md`** — domain glossary; W4 introduces `chain-fed handler`, `inter-handler data plane`, `${VAR} substitution`, `briefing surface` terms (added in §4 + §6 + §7 during W4 implementation)
- **ADR-013** (action registry), **ADR-014** (tool surface), **ADR-015** (user resolver), **ADR-016** (cron), **ADR-020** (chain validation), **ADR-021** (acceptance gate layering), **ADR-024..031** (W3 cohort) — all referenced by new ADRs
- **`docs/W2-VERIFICATION.md`**, **`docs/W3-VERIFICATION.md`** — extension points for **`docs/W4-VERIFICATION.md`** (added during W4)

### New ADRs grounding this PRD

| ADR | Title | Captures |
|---|---|---|
| ADR-018-amended | W4 reconsidered server-side LLM, stays agnostic | Updated context (ChatGPT Tasks exists, LangChain dominance); decision unchanged |
| ADR-032 | Secrets-aware action handlers + http_call env var substitution | Whitelist policy, literal detection, env-only secrets convention |
| ADR-033 | Inter-handler data flow via JobRun.result | `from_run_id` convention, recommended chain patterns, anti-pattern naming |
| ADR-037 | tasks://recent-results MCP resource as briefing surface | Schema, 50-row cap, user_id scoping, broken-loop fix |
| ADR-038 | mcp_call as future direction (Deferred) | Worker-as-MCP-client v2 design sketch + deferral rationale |
| ADR-039 | Plan abstraction as future direction (Deferred) | v2 schema (plans table, plan_id FK) + deferral rationale |
| ADR-040 | Predicate-based chain as future direction (Deferred) | jq/CEL/Lua evaluator candidates + deferral rationale |
| ADR-041 | Static landing page + Caddy path routing | Path namespace decision; future endpoint convention |
| ADR-042 | Postgres-backed rate limiting | DB-over-Redis trade-off; limitations; Redis upgrade triggers |
| ADR-044 | Project rename to task_scheduler_mcp | Vendor-neutral framing; PR execution sequence |
| ADR-045 | email_send action design | SMTP vs API-service trade-off; bounce handling |
| ADR-046 | r2_upload action design | R2 over S3; W3 backup cron evolution to typed action |
| ADR-048 | calendar_digest_ics action design | ICS vs OAuth trade-off; URL-as-token security model |

### Verification (W4 acceptance gates G1-G10)

W4 is "done" when G1-G10 all pass. G7 inherits W3's L6 (Fargate validation execution) and G8 inherits W3's L7 (visual artifacts) — both shift from W3 deferred to W4 must-ship.

- **G1 Code green**: all CI workflows green on `main`; new test suite at coverage targets; `terraform plan` green for any terraform changes
- **G2 Action sprint shipped**: all 5 new handlers in `app/actions/registry.py`; each handler has happy + main error tests passing; `task.list_actions.v1` returns 7 actions
- **G3 Digest workflow live**: real `github_digest → slack_post` chain firing weekday 9am Asia/Taipei on production VPS; Slack channel logs ≥ 5 consecutive workday messages (success or self-alert)
- **G4 Recent-results surface**: `tasks://recent-results` queryable via MCP Inspector against live VPS; returns real 24h data
- **G5 Landing page live**: `curl https://scheduler.paynepew.dev/` returns 200 + HTML containing project name + GitHub link
- **G6 Rate limiting active**: integration test demonstrates 1001st create returns rate-limit error; live demo URL has rate limit env vars set
- **G7 L6 Fargate evidence**: `validate-fargate.yml` dry + recording runs both green; evidence artifacts captured; total bill < $5
- **G8 Visual artifacts**: hero GIF + 4 screenshots + 3 architecture diagrams in repo; README renders them inline
- **G9 README polished + i18n**: `README.md` (English, ~150 lines, three-fold structure) + `README.zh-TW.md` (translation); i18n switcher in both files
- **G10 ADR cluster complete**: ADR-018-amended + 032, 033, 037, 038, 039, 040, 041, 042, 044, 045, 046, 048 all merged

### Risks and mitigations

- **Risk: rate limiting causes false-positive rejection for legitimate burst usage.** Mitigation: env var configurable; documented limitations in ADR-042; ops runbook for tuning.
- **Risk: literal secret detection has false negatives (unknown prefixes).** Mitigation: best-effort; documented in ADR-032 (no claim of completeness); README warning explicit.
- **Risk: `${VAR}` substitution in nested action_params has unforeseen recursion edge cases.** Mitigation: unit test matrix covers nested str/dict/list combinations; pre-flight at task.create.v1 catches malformed `${...}` syntax.
- **Risk: Calendar ICS URL leaks if README example uses real URL.** Mitigation: README uses placeholder `${GCAL_ICS_URL}`; never log resolved value; ADR-048 emphasizes URL-as-bearer-token security model.
- **Risk: Daily digest workflow fails silently for days before noticed.** Mitigation: Design B chain pattern (ADR-033) — `trigger_on_status=ANY` + slack_post error-path format means failure → Slack alert. Verified during G3.
- **Risk: L6 recording run fails mid-recording.** Mitigation: dry run pre-flight catches most issues; recording uses 180-min duration for retake budget; pre-captured dry-run evidence as fallback for L7 alt-artifacts.
- **Risk: Project rename breaks something subtle (forgotten reference, CI cache).** Mitigation: rename is the first slice; single PR sequence with explicit grep verification; old Docker tags retained for rollback.
- **Risk: W4 scope (~85-100h) exceeds available calendar.** Mitigation: critical path is rename → handlers → README → digest go-live (~30h); polish (landing page, rate limit, L6 tooling) deferrable as parallel tracks; doc-only ADR cluster lowest risk, can defer to last week.
- **Risk: Postgres COUNT performance degrades as jobs table grows.** Mitigation: ADR-042 documents trigger conditions for Redis upgrade; partial index on `(user_id, created_at)` added in W4 migration.

### Cost projection

| Phase | Monthly cost (USD) |
|---|---|
| W4 idle (W3 cost unchanged + R2 = same ~$6) | **~$6** |
| W4 daily digest GitHub API calls (free quota) | $0 |
| W4 daily digest Slack webhooks (free) | $0 |
| W4 calendar digest Google ICS (free) | $0 |
| W4 L6 dry run + recording run (one-shot) | < $3 one-time |
| W5+ optional: BYO Anthropic/OpenAI API per user workflow | User-paid, not platform cost |
| 12-month projected (job search active, post-W4) | **~$75** (vs W3-baseline $70-80) |

---

*Generated 2026-05-19 from Grilling Session #5 via `/to-prd`.*
