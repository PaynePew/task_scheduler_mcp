# Task Scheduler MCP

MCP-based job scheduler exposing `task.create / list / status / cancel / list_actions` tools backed by Postgres + SQS (ElasticMQ locally). See `docs/PRD/prototype-w1.md` for the W1 prototype spec.

## Agent skills

### Issue tracker

GitHub Issues via the `gh` CLI. Repo: `PaynePew/task_scheduler_mcp`. See `docs/agents/issue-tracker.md`.

### Triage labels

Default canonical vocabulary: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `CONTEXT.md` at the repo root; all design and decision docs live under `docs/` — ADRs in `docs/adr/`, PRDs in `docs/PRD/`, agent guides in `docs/agents/`. Legacy course / grilling-session notes still live under `doc/` (singular) and are background-only. See `docs/agents/domain.md`.
