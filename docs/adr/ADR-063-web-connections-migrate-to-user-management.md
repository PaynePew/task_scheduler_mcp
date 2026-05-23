# ADR-063: Migrate web `/connections` from `/sso/*` to `/user_management/*`

- **Status**: Accepted
- **Date**: 2026-05-23
- **Deciders**: PaynePew
- **Related**:
  - ADR-049 (public multi-tenant OAuth pivot — strategic motivation)
  - ADR-053 (Layer-1 = WorkOS AuthKit) — closes the addendum's "Proper fix (future)" loop
  - ADR-054 (Layer-2 token storage via KMS envelope) — affected DB schema
  - ADR-058 (Layer-2 connection UX — the dashboard this migration unblocks)
  - Issue #189 / PR #194 (interim `WORKOS_AUDIENCE` / `WORKOS_RESOURCE_URL` split)
  - Issue #203 (the P0 incident this ADR resolves)

## Context

The web `/connections` dashboard signed users in via WorkOS `/sso/*` while the
MCP HTTP server validated bearer tokens issued by WorkOS `/user_management/*`
(reached via CIMD/DCR per the 2025-11-25 MCP spec). Both flows write/read the
same column — `oauth_connections.user_id` — but **WorkOS issues distinct ID
schemas for the same human depending on which endpoint family minted the
token**:

| Endpoint family | `sub` schema | WorkOS entity |
|---|---|---|
| `/sso/*` (SSO Connection-bound) | `prof_01...` | Profile |
| `/user_management/*` (AuthKit global) | `user_01...` | User |

Symptom (issue #203, confirmed in production 2026-05-23): a user completes
`https://scheduler.paynepew.dev/connections` for GitHub/Slack/Google, all
three rows are written under `prof_01KS7YFTGASB271GPZSKRFYK7Z`. Claude Desktop
then connects via MCP, its bearer JWT carries `sub=user_01KS7YFV8C0AV020FA640N8Q0F`,
and `_check_oauth_connection` queries `oauth_connections WHERE user_id='user_01...'`
→ 0 rows → returns `MISSING_CONNECTION`. **No real user has successfully
created a job via the public MCP surface since the W4 OAuth pivot.**

ADR-053's 2026-05-23 addendum already documented this risk and prescribed
the migration as the "Proper fix (future)". This ADR captures the decision to
execute it now, the implementation shape, and the data-migration procedure
for pre-existing `prof_*` rows.

## Decision

The web `/connections` dashboard is migrated to the AuthKit **User Management**
endpoint family. WorkOS officially recommends this path: "When handling the
application callback, replace calls to the SSO Get a Profile and Token API
with the AuthKit Authenticate API, setting `grant_type` to `authorization_code`.
Note that the response provides a User object instead of a Profile, with
potentially different User IDs." (WorkOS migration guide.)

Concrete code changes (`app/web/connections.py`, `_connections_login` and
`_workos_auth_callback`):

| Concern | Before (SSO) | After (User Management) |
|---|---|---|
| Authorize URL | `{workos_issuer}/sso/authorize` | `{workos_api_base_url}/user_management/authorize` |
| Token-exchange URL | `{workos_issuer}/sso/token` | `{workos_api_base_url}/user_management/authenticate` |
| Provider hint | `provider=GitHubOAuth` (workaround so the code routed back through `/sso/token`) | `provider=authkit` (WorkOS-recommended; hosted AuthKit UI handles the chooser) |
| Token-response fallback (when access_token is opaque and id_token absent) | `token_data["profile"]["id"]` (SSO Profile id) | `token_data["user"]["id"]` (User Management User id) |
| Body of token POST | included `redirect_uri` (SSO required) | omits `redirect_uri` (User Management does not require it; exchange is `client_id` + `client_secret` + `code`) |

### Host distinction — `workos_api_base_url` vs `workos_issuer`

A subtlety caught only by the sandbox probe (issue #203 runbook §B.1, 2026-05-23):

- **`workos_issuer`** points at the AuthKit subdomain
  (`https://<tenant>.authkit.app`). This is the JWT `iss` claim value and the
  JWKS host. The AuthKit subdomain **aliases the legacy `/sso/*` paths** for
  backward compatibility — which is why the un-migrated code "just worked"
  with `{workos_issuer}/sso/authorize`. But it **does NOT alias `/user_management/*`**:
  hitting `https://<tenant>.authkit.app/user_management/authorize` returns
  HTTP 404.
- **`workos_api_base_url`** points at the canonical WorkOS REST API host
  (`https://api.workos.com` by default). This is the only host that serves
  `/user_management/*`. Empirical confirmation 2026-05-23:
  `https://api.workos.com/user_management/authorize?...` → 302,
  `https://api.workos.com/user_management/authenticate` (POST with bad body)
  → 400 (endpoint exists, payload rejected).

The PR therefore introduces a separate `workos_api_base_url` setting with a
sensible production default (`https://api.workos.com`) rather than reusing
`workos_issuer`. The two have always been semantically distinct; the SSO
alias on the AuthKit subdomain masked that fact and led to a false start in
the first migration attempt — captured here so the next reader does not
repeat it.

The Bearer-token validation path (`app/auth/token_validation.py`) is unchanged
— it already accepted whatever `sub` the WorkOS JWKS verified. The migration
simply makes both sides converge on the same schema.

`scope=openid profile email` is dropped from the authorize URL because
User Management does not require it; AuthKit returns the user identity in
the User object on the `/authenticate` response.

## Alternatives considered

- **Map `prof_*` → `user_*` at write/read time, keep SSO endpoints** —
  rejected. WorkOS does not expose a stable `prof → user` translation API
  for the SSO flow (it would require the SSO `getProfile` endpoint + correlation
  via email, racy if a user has multiple Connections). Reading WorkOS's own
  recommendation, the migration is the supported path.
- **Move MCP to `/sso/*` too** — rejected. CIMD/DCR (the MCP authorization
  mechanism) lives on `/user_management/*` by design; downgrading would also
  break RFC 8707 resource-indicator binding (issue #189).
- **Add a translation table `oauth_user_aliases (prof_id, user_id)`** — rejected.
  Adds permanent complexity for a one-time migration; WorkOS makes the right
  choice cheap.

## Consequences

### Code
- `_connections_login` redirects to `{workos_issuer}/user_management/authorize`
  with `provider=authkit`.
- `_workos_auth_callback` POSTs to `{workos_issuer}/user_management/authenticate`,
  parses the AuthKit User Management response shape (`user.id`), and writes
  session cookies keyed by the `user_*` sub.
- All three downstream OAuth handlers (GitHub / Slack / Google) keep storing
  connections under whatever `user_id` the session carries — they continue to
  work transparently because the session is now `user_*`.

### Tests
- New unit tests (`tests/unit/test_connections_web.py`) pin the new endpoint
  URLs, the `provider=authkit` hint, and the `user.id` fallback.
- New integration test
  (`tests/integration/test_oauth_e2e_web_then_mcp.py::test_web_oauth_callback_and_mcp_check_resolve_same_user`)
  exercises web flow + MCP `_check_oauth_connection` for the same mock user
  and asserts no `MISSING_CONNECTION`. Companion negative assertion shows that
  a `prof_*` sub WOULD miss — proving the regression net is wired correctly.

### Operator-only steps (NOT in this PR; must run before merge to prod)
1. **WorkOS dashboard — enable User Management for the AuthKit app** (if not
   already).
2. **WorkOS dashboard — Redirects page** — register
   `${CONNECTIONS_BASE_URL}/connections/auth/callback` as an allowed redirect
   URI under the User Management product (in addition to any existing SSO
   redirect URIs).
3. **Sandbox verification** — stand up a fresh WorkOS app (or clearly
   isolated environment); run web flow + MCP bearer flow end-to-end; confirm
   `oauth_connections.user_id` is `user_01...` and the MCP gate finds the row.
4. **Data migration for pre-existing `prof_*` rows** — see "Data migration"
   below.

The operator keeps a detailed step-by-step runbook (sandbox probes, exact
curl commands, migration SQL with safety checks) in their local
`.doc/learn/` directory (gitignored per repo policy — these are
personal notes, not committed). The essentials are captured in this ADR
under "Operator-only steps" above and "Data migration" below so any future
operator can reconstruct the steps from the public record.

### Data migration

At the time of writing, production `oauth_connections` has 3 rows for one
operator-user under `prof_01KS7YFTGASB271GPZSKRFYK7Z` (GitHub / Slack / Google).
No other real users have rows.

- **C1 — one-time SQL (chosen for the current row count)**:
  ```sql
  UPDATE oauth_connections
     SET user_id = 'user_01KS7YFV8C0AV020FA640N8Q0F'
   WHERE user_id = 'prof_01KS7YFTGASB271GPZSKRFYK7Z';
  ```
  Acceptable because:
    - Only the operator's rows exist.
    - The mapping `prof_01KS7YFTGASB271GPZSKRFYK7Z → user_01KS7YFV8C0AV020FA640N8Q0F`
      was established by direct observation (web row created at 2026-05-23 09:10,
      MCP log line at 09:56:55 — same human, same WorkOS-issued ULID prefix).
    - This is a one-shot operation, not a recurring data-pipe.
- **C2 — script (future-proofing)**: if other users have connected before the
  migration ships, write a one-shot `bin/migrate_prof_to_user.py` that, for
  each `user_id LIKE 'prof_%'` row, calls WorkOS to resolve the corresponding
  `user_*` ID and rewrites the column inside a transaction with a logged
  dry-run mode. **Not built yet** because C1 fits the current row count.

### Risk
- **WorkOS User Management product outage** would now 100% block web sign-in
  (today, an SSO product outage didn't, because we ran exclusively on SSO).
  Acceptable for a portfolio deployment; the operator's local stdio path is
  unaffected.
- **Single-source bug** (one provider can no longer serve as a hot fallback
  for the other) is the natural consequence of unifying the user_id schema,
  and that unification is the whole point of this ADR.

### Backout
- `git revert` of the migration commit + redeploy. The pre-existing `prof_*`
  rows (post-C1, none remain — they were rewritten to `user_*`) would orphan
  on revert; rolling back also requires `UPDATE oauth_connections SET user_id =
  'prof_01...' WHERE user_id = 'user_01...'`. Backout SQL kept in the runbook.

## Out of scope

- Migrating the bearer-token validation path; that already accepts whatever
  `sub` WorkOS hands it.
- Collapsing `WORKOS_AUDIENCE` + `WORKOS_RESOURCE_URL` back into one variable
  (the original ADR-053 plan): possible once dashboard-side RFC 8707 resource
  indicators are registered, but not required for this fix — keep the existing
  split until #194 is revisited.
- Action surface changes; OAuth-gated actions consume `_check_oauth_connection`
  which is unmodified.
