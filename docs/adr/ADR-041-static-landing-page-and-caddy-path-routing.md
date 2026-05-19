# ADR-041: Static landing page and Caddy path routing

**Status:** Accepted  
**Date:** 2026-05-19  
**Deciders:** PaynePew  
**Issue:** #97

---

## Context

`scheduler.paynepew.dev/` previously returned a raw 404 or empty response because Caddy proxied every request to `mcp-server:8000` and the MCP HTTP server only handles `/healthz` and `/mcp` paths.  Recruiters clicking the demo URL from a resume saw nothing useful.

This ADR documents three decisions made together to fix this:

1. A minimal static landing page (`index.html` + `style.css`) served directly by Caddy.
2. Path-based routing in the Caddyfile that formally partitions the namespace.
3. The rationale for each choice over the alternatives.

---

## Decision 1 — Path-namespace convention

The Caddyfile is changed from a single catch-all `reverse_proxy` to three explicit matchers:

```caddy
handle /healthz  { reverse_proxy mcp-server:8000 }
handle /mcp*     { reverse_proxy mcp-server:8000 }
handle /         { root * /var/www; file_server }
```

**Why:** The MCP server owns `/healthz` and `/mcp*`; everything else is unregistered namespace.  Making ownership explicit means any future endpoint (`/admin`, `/metrics`, `/api/v2`) must add a matcher — accidental catch-all proxying of new paths is impossible.

**Rejected:** leaving Caddy as a pure transparent proxy and adding a `/` handler inside the Python app.  That would couple the static-content concern to the MCP server process; the landing page would break if the server restarted.

---

## Decision 2 — No JavaScript, no frontend framework

`index.html` is a single self-contained file: semantic HTML5, one external stylesheet, no script tags.

**Why:**
- Zero attack surface (no eval, no XSS, no supply-chain dependency).
- Infinite HTTP cache eligibility (`Cache-Control: public, immutable` when deployed behind a CDN).
- Version-controlled and diff-readable; changing copy is a one-line PR.
- No build step required — `caddy file_server` serves it directly.

**Rejected:** React/Next.js landing page.  Completely disproportionate for a ~150-word hero page; would require a Node.js build step in CI and a separate container or CDN.

---

## Decision 3 — Static files in container, not CDN

The landing page is mounted into the Caddy container via a bind-mount (`./static:/var/www:ro`).  The files live in `infra/vps/static/` in the repo and are copied to the deploy root by `bin/setup-vps.sh`.

**Why:**
- Single deployment unit: `docker compose pull && docker compose up -d` updates everything including the landing page.  No separate CDN invalidation step.
- Matches the project's docker-compose ergonomics (ADR-029).
- Content-addressed caching still works: Caddy sets `ETag` headers based on file content.

**Rejected:** Cloudflare Pages / S3 + CloudFront.  Adds a second deployment target to monitor, a second set of credentials to rotate, and a second failure mode — all for a page that changes less than once per sprint.

---

## Dark/light mode

`style.css` uses `prefers-color-scheme` media query to auto-switch between a white background (light) and a near-black `#0f1117` background (dark).  System-font stack (`-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, …`) avoids web-font requests entirely.

---

## Hero asset

`index.html` references `hero.gif` relative to itself.  The canonical source of truth is `docs/diagrams/hero.gif` (created in the README / visual-artifacts issue).  `infra/vps/static/hero.gif` is a copy or symlink from that path; the HTML falls back gracefully (`onerror="this.style.display='none'"`) if the GIF is absent.

---

## Consequences

- `curl https://scheduler.paynepew.dev/` returns 200 + HTML containing the project name and GitHub link.
- `curl https://scheduler.paynepew.dev/healthz` continues to return 200 + JSON.
- MCP transport at `/mcp` is unaffected.
- Any future path that is not `/healthz`, `/mcp*`, or `/` returns 404 from Caddy until a matcher is added — this is intentional.
- `bin/setup-vps.sh` must be re-run (or the systemd unit restarted) to sync `static/` to the deploy root on existing VPS instances.
