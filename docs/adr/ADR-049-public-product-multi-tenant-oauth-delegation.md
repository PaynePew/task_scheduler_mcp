# ADR-049: Public product positioning — multi-tenant MCP via OAuth delegation

- **Status**: Accepted
- **Date**: 2026-05-20
- **Deciders**: PaynePew
- **Source**: Grilling Session #6 (grill-with-docs, 2026-05-20)
- **Related**: ADR-015 (user-identity resolver — reversed), ADR-024 (W3 OIDC defer — reversed), ADR-027 (VPS deployment), ADR-042 (rate limiting — must follow), ADR-044 (rename), ADR-050 (credential model), ADR-051 (action tiering)

## Context

ADR-015 / ADR-024 locked a **trust-only** auth posture: `user_id` comes from a
self-asserted `X-User-Id` header (or `MCP_USER_ID` env), with no validation.
That was correct under the operating assumption that **only the operator would
ever connect** — the public `/mcp` endpoint was a demo surface, not a product.

In Grilling Session #6 the deciders rejected that framing. The stated goal:

> "Anyone can connect to this MCP and safely use the provided actions with no
> security concerns; convenient low-friction auth; proper protections added —
> a showable *product*, even if few people use it."

This is a deliberate reversal. The word "使用者" (user) shifts meaning from
*the operator* to *arbitrary external end-users*. Trust-only auth is
incompatible with that goal: a self-asserted header means any caller can
impersonate any `user_id`, so per-user isolation is meaningless.

## Decision

**The public deployment becomes a multi-tenant product authenticated via OAuth
2.1 delegation, per the MCP authorization spec.**

- The MCP server is an OAuth 2.1 **Resource Server**. `user_id` = the verified
  token subject (`sub`), cryptographically trustworthy — not forgeable.
- User authentication is **delegated to a third-party identity provider** (the
  specific IdP — self-hosted vs Clerk/WorkOS/Stytch/Auth0 — is a follow-up ADR).
- Onboarding targets low friction via the MCP authorization flow — Client ID
  Metadata Documents (CIMD, the spec default since 2025-11-25) with Dynamic
  Client Registration (RFC 7591) as fallback — so consumer apps (ChatGPT /
  Claude.ai connectors) and developer CLIs (Claude Code, Codex) attach without
  manual app creation. IdP choice resolved in ADR-053 (WorkOS AuthKit).
- The trust-only `X-User-Id` / `MCP_USER_ID` path (ADR-015) **survives only**
  for local stdio (Claude Desktop) and the operator's own access — never for
  the public HTTP surface.

## Alternatives considered

- **Stay trust-only / "just me" (operator-only tool).** Recommended by the
  grilling assistant on cost/effort grounds; rejected by the deciders, who want
  a real product narrative over a personal tool.
- **Showcase-only (nobody connects; demo video + landing page only).**
  Rejected as hollow — the deciders want to actually use and demonstrate a
  working multi-tenant product.
- **Trust-only + rate limiting as the only protection (ADR-042 alone).**
  Rate limiting bounds job *count* but not *identity*; it cannot provide
  per-user isolation. Necessary but not sufficient.

## Consequences

- **Reverses ADR-015 and ADR-024's OIDC deferral.** Auth is now in scope.
- Enables genuine per-user isolation: all read/cancel paths filter by a
  *verified* `user_id`, closing the "set `X-User-Id` to someone else" hole.
- Forces a credential-ownership decision for actions that call downstream
  services — resolved in **ADR-050**.
- Forces an action-exposure decision (what a verified stranger may invoke) —
  resolved in **ADR-051**.
- Open follow-ups: IdP/implementation choice; OAuth token encryption-at-rest
  key management on the $5 VPS (no KMS); per-authenticated-user rate limiting
  (ADR-042 revision); cost/DoS containment.
- Acceptable scope cut: this is a "real but small" product. No horizontal-scale
  multi-tenancy, no enterprise SSO — the kanban-harness lesson (don't chase
  SaaS scale while job-searching) still holds; correctness and safety, not
  scale, are the bar.
