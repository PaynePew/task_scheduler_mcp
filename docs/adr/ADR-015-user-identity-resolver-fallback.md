# ADR-015: User identity — hybrid `X-User-Id` header → env var → `default-user` fallback

- **Status**: Accepted
- **Date**: 2026-05-12
- **Source**: internal grilling session Q15 (local-only, not in git)
- **Related**: ADR-005 (ALB front door), ADR-006 (dual transport)

## Context

Multi-tenancy must be a first-class concern from day 1 so the schema can carry `user_id` without future migration. But the auth backend differs per environment:

- W1 stdio (Claude Desktop) — no HTTP layer, env var is the only signal.
- W1 HTTP (local multi-user demo) — header is the only convenient signal.
- W3 cloud — ALB OIDC integration injects `x-amzn-oidc-identity` (Cognito sub claim).

## Decision

A **single resolver function** that determines `user_id` per request via the fallback chain:

1. If MCP request carries `X-User-Id` HTTP header → use it.
2. Else if `MCP_USER_ID` env var is set → use it.
3. Else → literal `"default-user"`.

In W1 this is **trust-only** — no validation, no signature, no JWT check. The W1 deployment surface is local only, behind no public endpoint.

**W3 upgrade path:** ALB OIDC authentication injects `x-amzn-oidc-identity`. The resolver function swaps step 1 to read that header instead. The schema column `user_id TEXT NOT NULL` is format-agnostic, so no migration required when the source changes.

**`job_id` choice:** `BIGSERIAL` (demo-friendly numeric IDs). W4 may add a separate `external_id ULID` column for public-facing references if needed.

## Alternatives considered

- **Hardcode single tenant** — kills the multi-user HTTP demo; defers all multi-tenancy work to W3.
- **JWT validation in W1** — premature; introduces a public-key fetcher, key rotation concerns, and key-management ops for no W1 user-facing benefit.
- **OAuth in W1** — same story, even heavier.
- **Physical partitioning of `jobs` by `user_id`** — premature; index on `(user_id, created_at DESC)` suffices at prototype scale (see ADR-009).

## Consequences

- `user_id` is present in `jobs` from day 1; all read paths filter by it.
- W3 auth swap is a one-function change — the resume narrative "lift-and-shift to ALB OIDC" is honest.
- W1's trust-only header is acceptable for a local demo but must be flagged as a non-goal — never expose this surface to the public internet without auth.
- Numeric `job_id`s are friendlier in demos than ULIDs but reveal scheduling volume; W4 ULID column is an option, not a requirement.
