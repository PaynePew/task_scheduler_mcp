# Connecting Task Scheduler MCP to Claude Desktop

> **Audience:** maintainers and power users. For a paste-able end-user version with no
> internal jargon, see [claude-desktop-quickstart.md](./claude-desktop-quickstart.md).

This guide covers connecting the scheduler to **Claude Desktop**. The same MCP endpoint also
works with Claude Code, Cursor, and MCP Inspector — see [README §3](../../README.md#§3-how-to-use)
for those clients.

---

## The mental model: two layers of authorization

The single biggest source of setup failures is conflating two *different* OAuth steps. They are
bound to the **same `user_id`**, so they must be done on the **same path** and **same account**.

```
You ──login①──▶ Scheduler itself (WorkOS AuthKit)      ← establishes WHO you are (user_id = JWT `sub`)
                     │
                     └──connect②──▶ GitHub / Slack / Google   ← so actions can call those APIs
```

- **Layer ① — connection auth.** Logging in to the scheduler when you add the connector. Uses
  WorkOS AuthKit (OAuth 2.1, PKCE/S256). Sets your `user_id` from the verified JWT `sub` claim
  ([ADR-053](../adr/ADR-053-layer1-authorization-server-workos-authkit.md)).
- **Layer ② — downstream provider auth.** Actions like `github_digest`, `slack_post`, and
  `email_send` call external APIs and need an upstream OAuth token. You grant these per-provider
  on the `/connections` dashboard ([ADR-049](../adr/ADR-049-public-product-multi-tenant-oauth-delegation.md),
  [ADR-058](../adr/ADR-058-layer2-connection-ux-dashboard-plus-link-surfacing.md)). Tokens are
  stored encrypted via AWS KMS envelope encryption
  ([ADR-054](../adr/ADR-054-layer2-token-storage-aws-kms-envelope-encryption.md)).

> **Without Layer ②**, an action returns `MISSING_CONNECTION` plus a `connect_url` and silently
> does nothing. The `connect_url` is built from the server's own `CONNECTIONS_BASE_URL` — if you
> log in on one path (e.g. Hosted) but try to connect providers on another (e.g. local), the two
> sides hold different `user_id`s and every action call fails.

---

## Prerequisites

- **Claude Desktop** (macOS / Windows), updated to the latest version.
- A **Claude Pro / Max / Team / Enterprise** plan — custom connectors are not on the free tier.
- Nothing to install or clone for the hosted path.

---

## Path A — Hosted (recommended, zero install)

Endpoint: `https://scheduler.paynepew.dev/mcp` · Dashboard: `https://scheduler.paynepew.dev/connections`

### 1. Add the custom connector

1. Claude Desktop → your name (bottom-left) → **Settings**.
2. **Connectors** in the left nav.
3. Scroll down → **Add custom connector** (or the **+** button).
4. Fill in:
   - **Name:** `Task Scheduler`
   - **Remote MCP server URL:** `https://scheduler.paynepew.dev/mcp`
   - **Advanced settings:** leave empty. The server supports **dynamic client registration**, so
     you do *not* need to supply an OAuth Client ID / Secret manually.
5. **Add**.

### 2. Sign in (Layer ①)

1. Click **Connect** on the connector. Claude opens your browser and runs the OAuth 2.1 flow
   (PKCE / S256) against WorkOS AuthKit.
2. Sign in and authorize.
3. Back in Claude Desktop the connector shows **Connected**, and the five `task.*` tools appear.

> **Under the hood:** Claude reads `/.well-known/oauth-protected-resource` (RFC 9728) → discovers
> the WorkOS authorization server → registers a client dynamically → receives a Bearer JWT, which
> it attaches to every `/mcp` request.

### 3. Connect downstream providers (Layer ②)

Skip only if you are testing with `echo`. Required for `github_digest` / `slack_post` / `email_send`.

1. Open `https://scheduler.paynepew.dev/connections`.
2. **Confirm the "Signed in as" identity matches the account from step 2** — this is where the two
   `user_id`s must align.
3. Click **Connect** for each provider you need: GitHub / Slack / Google.

### 4. Verify

In Claude Desktop, type:

```
List all my scheduled tasks
```

Claude should call `task.list.v1` (approve the tool-use prompt the first time).

Optional health check (run in your own terminal):

```bash
curl https://scheduler.paynepew.dev/healthz
# {"ok":true,"db":"connected"}
```

---

## Path B — Self-host (HTTP)

For running your own instance. MCP endpoint becomes `http://localhost:8000/mcp`; local auth is
**trust-only** via the `X-User-Id` header ([ADR-015](../adr/ADR-015-user-identity-resolver-fallback.md)).

```bash
git clone https://github.com/PaynePew/task_scheduler_mcp
cd task_scheduler_mcp
cp .env.docker.example .env.docker     # compose reads THIS
cp .env.example .env                   # host-side uv run (tests, alembic)
docker compose --profile full up -d
```

Claude Desktop has no native UI field for a custom `X-User-Id` header on a remote URL, so for a
local HTTP server use the `mcp-remote` stdio bridge in `claude_desktop_config.json`:

```jsonc
{ "mcpServers": { "task-scheduler": {
  "command": "npx",
  "args": ["mcp-remote", "http://localhost:8000/mcp", "--header", "X-User-Id:me"]
}}}
```

Then open `http://localhost:8000/connections`, confirm it shows `Signed in as me` (matching the
header), and Connect each provider.

> **Never expose trust-only `X-User-Id` to the public internet** — anyone can read another user's
> jobs by guessing the header. For an internet-facing deployment, set `CONNECTIONS_BASE_URL` to your
> public host and configure WorkOS Bearer auth (README §7).

---

## Path C — Self-host (stdio)

For MCP Inspector / short-lived dev only. Stdio MCP is a child process that dies with the chat, so
it cannot fire scheduled jobs while the client is closed ([ADR-006](../adr/ADR-006-mcp-transport-dual-stdio-http.md)).
The OAuth dashboard still lives in the HTTP web tier, so you still bring the stack up.

```jsonc
{ "mcpServers": { "task-scheduler": {
  "command": "uv",
  "args": ["run", "python", "-m", "app.entrypoints.mcp_stdio"],
  "env": { "MCP_USER_ID": "me", "MCP_USER_TZ": "UTC" }
}}}
```

> **The stdio gotcha:** the stdio process (`MCP_USER_ID` from this `env` block) and the web tier
> (`MCP_USER_ID` from `.env.docker`) must resolve to the **same** string, or you OAuth as one user
> and query as another.

---

## What you can do once connected

**Tools (5):** `task.create.v1` · `task.list.v1` · `task.status.v1` · `task.cancel.v1` · `task.list_actions.v1`

**Actions (7):** `echo` · `http_call` · `slack_post` · `github_digest` · `email_send` · `r2_upload` · `calendar_digest_ics`

Talk to it in natural language:

```
Every weekday at 9am Taipei time, post a summary of my GitHub notifications to Slack #standup
Email me@example.com a reminder titled "standup" in 30 minutes
What actions are available?          → task.list_actions
Cancel task <job_id>                 → task.cancel
```

Supports cron recurrence, job chaining (`trigger_on_job_id` — the previous run's output feeds the
next), and `${VAR}` env-var substitution. Rate limit: **1000 creates / 24h · 10 / min burst**
([ADR-042](../adr/ADR-042-postgres-backed-rate-limiting.md)).

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Action returns `MISSING_CONNECTION` + a `connect_url` | Layer ② not done, or done on a different account/path | Connect that provider at `/connections` under the **same** account |
| 401 / repeated re-login prompts | Token expired or wrong account | Disconnect → Connect the connector to re-run OAuth |
| No "Add custom connector" option | Free plan | Upgrade to Pro or above |
| Tools don't appear | Connector not Connected, or client not restarted | Finish step 2; restart Claude Desktop if needed |
| `connect_url` shows `localhost` on a hosted setup (or vice-versa) | Mixed paths — MCP on one path, OAuth on another | Pick one path end-to-end |

---

## References

- [README §3 — How to use](../../README.md#§3-how-to-use)
- [ADR-006 — Dual stdio + HTTP transport](../adr/ADR-006-mcp-transport-dual-stdio-http.md)
- [ADR-049 — Public multi-tenant OAuth delegation](../adr/ADR-049-public-product-multi-tenant-oauth-delegation.md)
- [ADR-053 — Layer 1 authorization server (WorkOS AuthKit)](../adr/ADR-053-layer1-authorization-server-workos-authkit.md)
- [ADR-054 — Layer 2 token storage (AWS KMS envelope encryption)](../adr/ADR-054-layer2-token-storage-aws-kms-envelope-encryption.md)
- [ADR-058 — Layer 2 connection UX](../adr/ADR-058-layer2-connection-ux-dashboard-plus-link-surfacing.md)
