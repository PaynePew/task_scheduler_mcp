# ADR-059: Rollout — operator-trusted + public-OAuth coexistence, `default-user` migration

- **Status**: Accepted
- **Date**: 2026-05-21
- **Deciders**: PaynePew
- **Source**: Grilling Session #6 (grill-with-docs)
- **Related**: ADR-049 (public pivot), ADR-053 (WorkOS Layer-1), ADR-015 (trust-only — retained for operator stdio), ADR-006 (dual transport), ADR-055 (operator identity)

## Context

The system is live as trust-only single-tenant; all existing jobs are under
`user_id="default-user"`; transports are stdio + HTTP(`X-User-Id`). The public
OAuth multi-tenant pivot (ADR-049) must land without breaking the operator's own
usage. In practice there are **no external users yet**, so cutover risk is low.

## Decision

**Dual-path coexistence + a one-time data migration.**

- **Operator (trusted path):** local **stdio** continues, with
  `MCP_USER_ID` set to the operator's **WorkOS `sub`** so the operator's identity
  is consistent across stdio and OAuth. This is the `OPERATOR_USER_ID`
  (ADR-055/051 exemptions).
- **Public (OAuth path):** the HTTP `/mcp` endpoint **requires OAuth**
  (ADR-053); the trust-only `X-User-Id` path is **removed from public HTTP**
  (it was the security hole). Trust-only survives only for local stdio.
- **One-time migration:** reassign existing `default-user` rows
  (`jobs`, `job_runs`) to `OPERATOR_USER_ID` (the operator's WorkOS `sub`).
- **Simple cutover:** no canary/staged rollout — there are no external users to
  stage for. The build itself ships in PRD-sequenced waves; the public-auth wave
  does not disrupt the operator's stdio usage.

## Consequences

- A one-off data migration (Alembic data migration or script) for the `user_id`
  reassignment.
- `MCP_USER_ID` convention changes to "the operator's WorkOS `sub`".
- HTTP transport auth changes from trust-only to OAuth-required (ADR-053);
  stdio remains trust-only and local-only (ADR-006/015).

## Alternatives considered

- **Force OAuth for the operator too (no stdio path)** — rejected: the operator
  loses the zero-friction local path for no security gain (stdio is local-only).
- **Keep trust-only HTTP alongside OAuth** — rejected: that *is* the public
  security hole ADR-049 closes.
- **Canary / staged rollout** — rejected: no external user base to stage for;
  unnecessary complexity.
