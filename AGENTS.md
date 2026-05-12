# ChatGPT Task Scheduler

MCP-based job scheduler exposing `task.create / list / status / cancel / list_actions` tools backed by Postgres + SQS (ElasticMQ locally). See `doc/PRD/prototype-w1.md` for the W1 prototype spec.

## Agent skills

### Issue tracker

GitHub Issues via the `gh` CLI. Repo: `PaynePew/chatgpt_task`. See `docs/agents/issue-tracker.md`.

### Triage labels

Default canonical vocabulary: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `CONTEXT.md` + `docs/adr/` at the repo root. Note: existing course/PRD docs live under `doc/` (singular); agent-facing docs live under `docs/` (plural). See `docs/agents/domain.md`.
