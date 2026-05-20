# ADR-051: Action surface tiering — public OAuth-able vs operator-only

- **Status**: Accepted
- **Date**: 2026-05-20
- **Deciders**: PaynePew
- **Source**: Grilling Session #6 (grill-with-docs, 2026-05-20)
- **Related**: ADR-013 (action catalog), ADR-049 (multi-tenant pivot), ADR-050 (credential model), ADR-046 (r2_upload), ADR-045 (email_send), ADR-018-amended (no server-side LLM), ADR-019 (LLM via client / http_call)

## Context

Given the dual credential model (ADR-050), each registered action must be
classified by which credential track it can use, hence who may invoke it:

- An action backed by a per-user **OAuth connection** is safe for public users.
- An action that reads operator `${VAR}`-env secrets must be **operator-only**,
  or it lets a stranger spend the operator's accounts (ADR-050 exfiltration).

"Operator-only" is **not a separate codebase** — the handlers and the `${VAR}`
resolver already exist. It is a one-line authorization gate: a `requires_operator`
flag on the action, checked at `task.create` against whether the caller's
verified `user_id` is the operator.

## Decision

### Action tiers

| Action | Tier | Credential track |
|---|---|---|
| `echo` | public | none |
| `github_digest` | public | GitHub OAuth connection (replaces `${GITHUB_TOKEN}`) |
| `slack_post` | public | Slack OAuth connection — chat:write (replaces `${SLACK_WEBHOOK_URL}` webhook) |
| `email_send` | public | **rewritten to Gmail OAuth** (`gmail.send`); the SMTP variant is operator-only |
| `calendar_digest_ics` | public *or* operator-only | Google Calendar OAuth if promoted; stays operator-only if it keeps the secret ICS-URL form |
| `http_call` (arbitrary key) | operator-only | `${VAR}`-env escape hatch |
| `r2_upload` | operator-only → **demote** | see below |

### `r2_upload` → demote to cron

`r2_upload` exists to back up nightly `pg_dump` to R2. Before W4 it *was* a
shell script (ADR-046); W4 promoted it to a typed action. Under the public
pivot it has no public use case (it writes the operator's bucket). **Demote it
back to a VPS cron / shell script, off the MCP surface entirely.** This removes
it from the product action catalog rather than maintaining it as operator-only
glue inside MCP.

### `requires_operator` gate

Operator-only actions carry `requires_operator: true` in the registry.
`task.create` rejects them for delegated users with the existing
`INVALID_STATE` / `USER_INPUT` envelope. The `${VAR}` resolver (ADR-032) runs
unchanged for these — safe now because only the key's owner can trigger them.

## Resolved sub-decision — server-side LLM = (b) operator-subsidized + caps

"Summarize financial news / polish text" needs an LLM **at execution time** (no
client is connected at 08:00). **Decision: (b) operator-subsidized public.**
Public users may invoke LLM-backed actions powered by the *operator's* key.
This does **not** reintroduce the secret-custody problem (the key stays in env;
no stranger secret is held) — the cost moves from *security* to *operator token
spend*, made bounded by four mandatory caps:

1. **Pinned cheap model** — users cannot choose the model.
2. **Per-user token budget** — daily token ceiling per `user_id`.
3. **Input-size cap** — max tokens accepted per call.
4. **Global monthly hard ceiling** — provider-side usage limit (hard stop, e.g.
   $10) as the final backstop even if 1–3 fail.

Rejected: (a) operator-only — public AI features make the product materially
more compelling (the PDF's flagship "daily financial-news summary" becomes a
real public feature) and (b) is provably safe on secret-custody grounds.

**Follow-up (ADR-052):** the AI-action *design* — one fixed operator-authored
system prompt per typed AI action, with per-user variation expressed as
constrained params. A free-form user-supplied system prompt is forbidden: on the
operator's key it would be an abusable open AI proxy (cost blowup + provider
ToS/abuse liability + prompt injection).

## Consequences

- Most of the W4 typed-action catalog **upgrades** to OAuth (github, slack,
  email→Gmail) rather than being cut; the showcase narrative survives and
  strengthens (identity-aware, per-user OAuth).
- A "every Friday 08:00 fetch GitHub issues → email/Slack" workflow is fully
  achievable public + OAuth-only + zero LLM (fetch + template + send are
  deterministic). SMTP ≠ email: Gmail OAuth covers the OAuth-able majority;
  non-Google senders stay operator-only.
- `r2_upload` leaves the action catalog; backups become ops glue (cron).
- The LLM placement is resolved as **(b)** operator-subsidized + caps; the
  AI-action prompt/param design is tracked in ADR-052.
