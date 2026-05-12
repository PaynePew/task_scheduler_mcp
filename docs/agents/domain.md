# Domain docs

Single-context layout. Skills that consume domain language read these
files at the repo root:

| File | Purpose |
|---|---|
| `CONTEXT.md` | Domain glossary and language for this project. Not yet created — skills should treat its absence as "no domain context yet" rather than erroring. |
| `docs/adr/` | Architecture Decision Records. Not yet created. |

## Existing documentation under `doc/` (singular)

Course materials, prototype PRDs, and grilling sessions live under
`doc/` (singular):

- `doc/PRD/` — product requirements (e.g. `prototype-w1.md`)
- `doc/learn/` — course material study notes (system-design, mcp-protocol,
  aws-deep-dive, course-spec, interview-questions)
- `doc/session/` — grilling-session decision logs (`grilling-state.md`)

When a skill needs project context, it should read `CONTEXT.md` first (if
it exists), then fall back to the PRD under `doc/PRD/` and the
decision log under `doc/session/`.

## Reading rules

- Read `CONTEXT.md` whenever a skill needs domain vocabulary.
- Read every file under `docs/adr/` whose subject overlaps the area being
  changed — ADRs encode prior decisions and the reasoning to revisit them.
- Existing `doc/learn/*.md` files are study notes, not authoritative; use
  them as background, not as a substitute for `CONTEXT.md` or ADRs.

## Multi-context (not used)

This repo is not a monorepo. If that ever changes, replace this file with
`CONTEXT-MAP.md` at the root and create per-context `CONTEXT.md` files.
