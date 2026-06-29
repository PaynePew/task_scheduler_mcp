# ADR-066 — Owl brand layer + `owl-scheduler` routing identity

**Date:** 2026-06-29
**Status:** Accepted
**Deciders:** PaynePew
**Relates / amends:** ADR-044 (project rename to `task_scheduler_mcp`), ADR-061 (server self-description as onboarding/routing surface), ADR-014 (MCP tool surface v1)

---

## Context

On Claude Desktop, when the operator dispatched a task in natural language ("schedule a task to …"), the model (Opus) routed to **Claude's first-party built-in scheduler instead of this MCP server**, then replied that the requested capability didn't exist. This is a *routing-capture failure*, not a cosmetic branding problem: the server got zero tool calls (the same failure class as the ADR-061 incident, where stale `instructions` caused the LLM to route around the server).

Root cause: the server's routing-visible name was `task-scheduler` (`Server("task-scheduler", …)` and the suggested `claude_desktop_config.json` key), whose token *"task scheduler"* overlaps 100% with the built-in. The operator had no collision-free word to force routing to this server.

A naïve fix — "rename the product to *Owl Task Scheduler MCP* everywhere (README, GitHub repo, MCP config)" — conflates three different surfaces with very different cost/effect:

- **Human brand** (README, landing) — the LLM never reads it during a session; zero effect on routing.
- **Infra identity** (GitHub repo, image, `/opt` path, Terraform) — invisible to users; ADR-044 churned all of this only ~6 weeks prior; re-renaming is pure cost with zero routing effect.
- **LLM routing surface** (server self-name, suggested config key, `instructions`, tool descriptions) — the *only* surface that affects capture.

A further subtlety: dropping the token `task` from the routing name could *reduce* capture (generic "schedule a task" would then match only the built-in) unless the `instructions` actively claim the territory. So renaming alone is necessary-but-insufficient.

## Decision

### 1. Three decoupled names (scope = brand + routing surface; **not** infra)

| Axis | Value | Action |
|---|---|---|
| Display name (brand) | `Owl Task Scheduler MCP` | Update README (EN + zh-TW), landing page, OG/twitter metadata, `pyproject` description, docs prose, `CONTEXT.md` title |
| Routing identifier | `owl-scheduler` | `Server("owl-scheduler", …)`; suggested `claude_desktop_config.json` key in landing + onboarding guides |
| Infra identifier | `task_scheduler_mcp` (unchanged) | Keep repo / image / `/opt` path / Terraform tags / R2 buckets / Python dist name `task-scheduler-mcp` / bearer realm `task-scheduler-mcp` |

`owl` is already the established visual brand (owl mark + `owl.svg`, rebrand `c916f4a`); this formalises it into the name.

### 2. Tool surface unchanged

`task.*.v1` tool names and the `tasks://` resource scheme stay (versioned API contract, ADR-014). The `owl-scheduler` server namespace already disambiguates them (`owl-scheduler › task.create.v1`), and the `task` token in tool names correctly *attracts* scheduling intent once the server is distinctly "owl".

### 3. Routing capture lives in `instructions` + tool descriptions, not in renaming alone

- **`instructions` (ADR-061 surface):** opening line names the new identity and adds a **capability-based routing directive** — *for any request to run a registered action or to schedule/automate/recur/chain work, use the `task.*` tools here rather than a built-in scheduler, which cannot run these actions.* Differentiation is **capability-based**, not "persistent/server-side" (which over-claims hosted prod and excludes immediate/one-shot jobs) and not by naming the competitor (brittle). The ADR-061 test constraints are preserved: the "check the action list" directive and the `/connections` URL remain.
- **Tool descriptions:** kept factual and deployment-neutral — e.g. `task.create.v1` describes running a registered action *immediately, once, or on a recurring cron schedule*, with chaining/cancellation; it must not say "persistent server-side" (self-hosters run it locally; one-shot/immediate jobs exist). Description text is not part of `inputSchema`, so this is not an ADR-014 `.v1` contract change.
- **`owl` as an explicit invocation handle:** onboarding docs teach users a collision-free sentence ("ask Opus: *use owl-scheduler to post a GitHub digest to Slack every weekday 9am*") as the reliable manual override.

## Alternatives Considered

- **Full re-rename to `owl_*` (infra too).** Rejected: ADR-044 renamed infra 6 weeks earlier; another churn of image paths / `/opt` / CI / Terraform has zero effect on the routing collision.
- **Routing id `owl` (bare) or `owl-task-scheduler`.** `owl` loses the "what it does" cue; `owl-task-scheduler` keeps the colliding `task` token. `owl-scheduler` keeps the distinctive lead token and the purpose word while dropping bare `task`.
- **Rename tool names to `owl.*.v1`.** Rejected: breaks the ADR-014 versioned contract and the per-thread `tools/list` cache for marginal gain once the server namespace is `owl-scheduler`.
- **Disambiguate by naming Claude's built-in scheduler in `instructions`.** Rejected: brittle (the built-in can rename/evolve) and awkward to hardcode a competitor; capability-based framing is robust and also tells the LLM *when* to pick this server.

## Consequences

**Positive:**
- A collision-free routing handle (`owl-scheduler` / "owl") the operator can say to force routing here.
- Capture improves via three reinforcing routing signals (server name + `instructions` directive + tool descriptions) while the human brand and infra identity stay stable.
- No API-contract churn (tool names, resource scheme, schemas all unchanged).

**Negative / honest caveat:**
- **MCP server `instructions`/descriptions may not fully override a *first-party* built-in scheduler**, which can sit higher in the client's own system prompt. These changes will improve capture but are **not guaranteed** to win 100%. The explicit `owl` invocation handle is the reliable fallback. If capture is still poor after deploy, escalate by promoting an MCP **prompt** (`setup_summary`) as an explicit slash-command entry that bypasses routing competition entirely.

**Verification (required after deploy):** run a set of natural-language scheduling phrases on Claude Desktop and measure how often routing lands on `owl-scheduler` vs the built-in. Treat this ADR's "fix" as confirmed only once that test passes.

**Reversibility:** brand strings and `instructions` prose are cheap to revert; the `owl-scheduler` server self-name is a one-line change. The deliberate *non-action* (keeping `task_scheduler_mcp` infra) is what future contributors must not "tidy up" — hence the §0 entry in `CONTEXT.md`.
