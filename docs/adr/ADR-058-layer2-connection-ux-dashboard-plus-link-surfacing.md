# ADR-058: Layer-2 connection UX — web dashboard + just-in-time link surfacing

- **Status**: Accepted
- **Date**: 2026-05-21
- **Deciders**: PaynePew
- **Source**: Grilling Session #6 (grill-with-docs)
- **Related**: ADR-049 (public pivot), ADR-050/054 (connections + token storage), ADR-053 (WorkOS web login), ADR-041 (Caddy static + path-routing ethos)

## Context

Public users need a way to connect their GitHub / Slack / Google accounts
(Layer-2, ADR-050). Two patterns:

- **A — web connections dashboard**: a `/connections` page with Connect buttons.
  Mature, conventional (Zapier / Pipedream / Composio style), works with any
  client. Weakness: **discoverability** — the MCP client does not auto-open the
  page, so the user must know the URL.
- **B — in-chat URL elicitation**: the MCP server returns a connect URL in-chat
  just-in-time. Spec mechanism added 2025-11-25; **new, uneven client support,
  and it only covers the connect moment, not listing/revoking**.

## Decision

**A + just-in-time link surfacing.**

- A server-rendered, minimal `/connections` page (no JS framework, per ADR-041
  ethos), behind a WorkOS web-session login. Lists connections; Connect /
  Disconnect per provider. Each provider is its own OAuth consent (connecting is
  inherently one-provider-at-a-time).
- **Discoverability** fixed by surfacing the connect URL in: (a) the landing
  page, (b) MCP **tool/error responses** when a required connection is missing
  (envelope gains an optional `connect_url` hint), (c) the connector instructions.
- **Revocation** via `/connections` **and** provider-side (GitHub/Slack/Google
  settings) — an OAuth property, so the user is never locked in even if they lose
  the dashboard URL.
- True URL elicitation (B) deferred to v2 when client support matures; the
  link-surfacing above already gives most of B's contextual convenience without
  depending on the still-settling spec.

## Consequences

- A small web surface: `/connections` + per-provider OAuth callback routes; Caddy
  path-routing extended (ADR-041) with `/connections*` and the callback path.
- The tool error envelope (CONTEXT §6) gains an optional `connect_url` field.
- Reuses WorkOS for the web session (ADR-053) and the connection/token store
  (ADR-054).

## Alternatives considered

- **Pure A** — rejected: discoverability gap.
- **Pure B (URL elicitation only)** — rejected: spec immature, and it doesn't
  cover connection management/revocation.
- **A + full B now** — rejected: overbuild while B's client support is immature.
