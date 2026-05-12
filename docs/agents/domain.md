# Domain docs

Single-context layout. Skills that consume domain language read these
files at the repo root:

| File | Purpose |
|---|---|
| `CONTEXT.md` | Domain glossary and language for this project. |
| `docs/adr/` | Architecture Decision Records (ADR-001..ADR-015). |
| `docs/PRD/` | Product requirements (e.g. `prototype-w1.md`). |
| `docs/agents/` | Agent-facing process docs (this file, issue-tracker, triage-labels). |

## Legacy course material under `doc/` (singular)

Earlier course / grilling-session artefacts still live under `doc/`
(singular) for historical reasons. They are background-only — do NOT
treat them as authoritative when they disagree with `CONTEXT.md` or an
ADR:

- `doc/learn/` — course study notes (system-design, mcp-protocol,
  aws-deep-dive, course-spec, interview-questions)
- `doc/session/` — grilling-session decision logs (`grilling-state.md`,
  cited by ADRs as their `Source:` line)

When a skill needs project context, it should read `CONTEXT.md` first,
then the PRD under `docs/PRD/`, then relevant ADRs under `docs/adr/`.
Fall back to `doc/session/grilling-state.md` only when tracing *why* an
ADR was decided the way it was.

## Reading rules

- Read `CONTEXT.md` whenever a skill needs domain vocabulary.
- Read every file under `docs/adr/` whose subject overlaps the area being
  changed — ADRs encode prior decisions and the reasoning to revisit them.
- `doc/learn/*.md` files are study notes, not authoritative; use them as
  background, not as a substitute for `CONTEXT.md` or ADRs.

## Multi-context (not used)

This repo is not a monorepo. If that ever changes, replace this file with
`CONTEXT-MAP.md` at the root and create per-context `CONTEXT.md` files.
