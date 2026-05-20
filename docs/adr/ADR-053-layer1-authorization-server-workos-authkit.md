# ADR-053: Layer-1 authorization server = WorkOS AuthKit (managed IdP)

- **Status**: Accepted
- **Date**: 2026-05-21
- **Deciders**: PaynePew
- **Source**: Grilling Session #6 (grill-with-docs)
- **Related**: ADR-049 (multi-tenant pivot — resolves its open IdP follow-up), ADR-050 (Layer-2 downstream connections), ADR-015 (trust-only resolver — superseded on the public surface), ADR-042 (rate limiting), ADR-027 (VPS RAM budget)

## Context

ADR-049 made the public deployment a multi-tenant OAuth 2.1 product but left the
Authorization Server (AS) choice open. **Two distinct OAuth layers exist; this
ADR is only about Layer 1.**

| Layer | Scope |
|---|---|
| **Layer 1** (this ADR) | MCP client ↔ our server: authenticate the **end-user**, issue tokens for our resource server |
| **Layer 2** (ADR-050) | our server ↔ GitHub / Slack / Google: per-user **downstream** connections (separate OAuth apps registered with each provider) |

Spec note: since the **2025-11-25** MCP revision, **Client ID Metadata Documents
(CIMD)** is the default client-onboarding mechanism, with **Dynamic Client
Registration (DCR, RFC 7591)** as fallback. Anonymous DCR carries a
client-registration-flood / DDoS risk; CIMD avoids it.

## Decision

**Layer-1 AS = WorkOS AuthKit (managed IdP).** Our MCP server is an **OAuth 2.1
Resource Server only** — we do not run an Authorization Server.

WorkOS provides: end-user authentication (incl. social-login federation), token
issuance, and MCP-authorization support with CIMD/DCR endpoints (enabled via a
dashboard toggle).

We build the **resource-server side** (this is where the real work and the
portfolio signal live):

- **Protected Resource Metadata** endpoint (RFC 9728) + `WWW-Authenticate` 401
  challenge pointing clients at WorkOS.
- **Access-token validation**: verify the JWT signature against the WorkOS JWKS;
  check audience / `resource` binding (RFC 8707) so a token minted for a
  different resource cannot be replayed here (confused-deputy defense).
- `user_id` = verified token `sub`. This replaces the trust-only `X-User-Id`
  (ADR-015) on the public surface.

## Alternatives considered

- **Self-host Keycloak** — rejected: ~0.6–1 GB JVM RAM exceeds the $5 VPS budget
  (ADR-027 already uses 0.7–0.9 GB of 2 GB); incomplete RFC 8707; ops + security
  burden of running an AS.
- **Roll our own AS** — rejected: security anti-pattern.
- **Microsoft Entra ID** — rejected: no DCR support (a deliberate product
  decision), poor fit for zero-config MCP onboarding.
- **Descope** — a viable equivalent; AuthKit chosen for free-tier generosity and
  out-of-the-box MCP/DCR toggle.

## Consequences

- New external dependency (WorkOS) + a free-tier ceiling; a WorkOS outage takes
  public auth down (acceptable for portfolio; the operator's own local stdio
  path is unaffected).
- ADR-042 rate limiting now keys off a **verified** `user_id` (real per-user
  limits, not a shared trust-only identity) — tracked as an ADR-042 revision.
- Public PRM / challenge endpoints invite some abuse (registration flooding);
  mostly handled by WorkOS, but worth hardening.
- Cost: $0 within the free tier.
- Does **not** solve Layer-2 downstream-token storage / encryption-at-rest —
  the next open node.
