# ADR-032: Secrets-Aware Action Handlers and `${VAR}` Env-Var Substitution

- **Status**: Accepted
- **Date**: 2026-05-19
- **Deciders**: PaynePew
- **Related**: ADR-013 (action catalog), ADR-019 (LLM bonus via client-LLM integration), #93

## Context

Typed action handlers such as `slack_post`, `github_digest`, and `email_send` require
credentials at execution time (Slack webhook URLs, GitHub tokens, SMTP passwords). The
system has two options for making those values available:

1. **Store credentials in the database** alongside job definitions.
2. **Keep credentials in the server environment** and reference them symbolically in job
   definitions.

The current `http_call` handler is also the primary path for user-brought LLM integration
(ADR-019). Users authoring LLM API calls via `http_call` would naturally include their
API key as a header value. Without a substitution mechanism, users must choose between
embedding the key as a literal string in `action_params` (written to the `Job` row) or
abandoning `http_call` for their use case.

Four properties drive the design:

**P1 – DB compromise scope.** If credentials are stored in the database, a SQL injection
or DB backup leak exposes all credentials from all users. Env-only secrets limit blast
radius to host compromise.

**P2 – Typed handlers over `http_call` for routine integrations.** `http_call` with
a literal credential is both more dangerous (P1) and operationally awkward than a typed
handler (e.g. `slack_post`) that reads its credential from a known env var. Typed handlers
provide schema validation, retry semantics tuned to the service, and a clear audit log.
`http_call` should be reserved for one-off or custom integrations.

**P3 – Audit log hygiene.** Workers store `action_params` in `JobRun` rows for
post-mortem inspection. Literal credentials in those rows are a secondary leak surface
even if the DB is not directly compromised (e.g. logging, APM, or export pipelines).

**P4 – User ergonomics.** An LLM-authored job definition for `http_call` will
naturally include an API key. The system needs a symbolic form users can write
(`${ANTHROPIC_API_KEY}`) that communicates intent without embedding the value.

## Decision

### 1. Env-only secrets convention

Credentials are never stored in the database. All action parameters must reference
credentials via `${VAR_NAME}` syntax. The raw template strings (not the resolved values)
are stored in `Job.action_params` and `JobRun` audit fields.

### 2. `${VAR}` whitelist mechanism

`app/secrets/resolver.resolve(value, env, whitelist)` recursively replaces `${VAR}`
tokens in `str`/`dict`/`list` structures. Only variables in the *whitelist* may be
substituted:

- **Default whitelist**: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY`,
  `SLACK_WEBHOOK_URL`, `GITHUB_TOKEN`.
- **Operator extension**: `ALLOWED_TEMPLATE_VARS=MY_VAR,ANOTHER_VAR` env var appends
  to the default whitelist without code changes.
- **Unlisted var**: raises `SecretResolutionError(retryable=False)` — a permanent
  failure surfaced in `JobRun.error`. Retrying will not help; the operator must either
  add the var to `ALLOWED_TEMPLATE_VARS` or use a whitelisted name.

### 3. Literal-secret detection escape hatch

`app/secrets/literal_detection.detect_literal_secret(value)` walks `action_params` at
`task.create.v1` time and matches known credential prefixes:

| Prefix | Credential type |
|--------|-----------------|
| `sk-ant-` | Anthropic API key |
| `sk-` | OpenAI key |
| `xoxb-` | Slack bot token |
| `ghp_` | GitHub personal access token |
| `glpat-` | GitLab personal access token |
| `AIza` | Google API key |

On match, `task.create.v1` returns a `USER_INPUT` error envelope with `field=action_params`
and `expected="${VAR_NAME} reference instead of a literal credential"`, guiding the user
to the `${VAR}` form without revealing what was detected.

Detection is **best-effort**. The prefix list covers the most common credentials seen in
integration workflows. It makes no claim of completeness — novel credential formats,
hex-encoded values, or base64-wrapped keys will not be caught. The mechanism is a
UX guardrail, not a security boundary.

### 4. http_call resolver integration

`HttpCallHandler.execute()` calls `resolve()` on `headers`, `url`, and `body` before
opening any HTTP connection. Resolved values are used for the request; the raw
`HttpCallParams` object (with `${VAR}` strings intact) is what the worker persists to
`JobRun`. A `SecretResolutionError` causes an immediate `ActionResult(ok=False,
retryable=False)` — no network I/O occurs.

### 5. DB/SSH/monitor tokens are never whitelisted

`DATABASE_URL`, `DATABASE_PASSWORD`, `SSH_PRIVATE_KEY`, and similar infrastructure
credentials are intentionally excluded from the whitelist and from `ALLOWED_TEMPLATE_VARS`
convention (though an operator could add them). The scheduler has no legitimate use case
for routing infrastructure credentials through user-authored job parameters. If a custom
integration genuinely requires database access, a typed handler with its own credential
management should be built.

## Alternatives considered

### A. Store credentials encrypted in the database

Each user's credentials encrypted with a per-tenant key, stored as a `Secrets` table.
Rejected because:
- Adds key management complexity (key rotation, KMS dependency, or manual secret).
- `task.create.v1` would need a credential upsert path.
- Still vulnerable if the decryption key is co-located with the database.
- Out of scope for W4; env-only achieves P1–P4 without the complexity.

### B. Vault / secrets manager integration

Reference credentials by path in Vault, AWS Secrets Manager, or similar.
Rejected because:
- Adds external dependency; local dev requires a Vault sidecar.
- Overkill for a portfolio / small-team deployment.
- The whitelist mechanism achieves the same result for known credentials.

### C. No substitution — only typed handlers for secrets

Require all credential-using integrations to use typed handlers; `http_call` remains
credential-free. Rejected because ADR-019 explicitly supports user-brought LLM
integration via `http_call`, and blocking API key usage there undermines that goal.

## Consequences

**Positive**

- DB compromise does not expose user API keys.
- `JobRun` audit rows never contain credential values.
- `ALLOWED_TEMPLATE_VARS` gives operators a zero-code extension point.
- Literal-secret detection provides a UX guardrail at create time.

**Negative / trade-offs**

- The operator must set env vars before deploying secrets-aware jobs; missing vars
  cause permanent job failures (not retryable).
- Whitelist default covers 5 common vars; new integrations require either adding to
  `ALLOWED_TEMPLATE_VARS` or code changes.
- Literal detection is best-effort; users with unusual credential formats receive no
  guardrail.
- `http_call` can only reference vars the server knows about; it cannot access
  caller-side env vars (by design — the server has no knowledge of the MCP client's
  environment).
