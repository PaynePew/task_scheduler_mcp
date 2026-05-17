# ADR-028: Caddy 2 over nginx for the VPS HTTPS reverse proxy

- **Status**: Accepted
- **Date**: 2026-05-17
- **Deciders**: PaynePew
- **Source**: Grilling Session #4, Q-W3-5
- **Related**: ADR-027 (deployment target pivot to VPS), ADR-005 (ALB for the Fargate target)

## Context

The Lightsail VPS (ADR-027) needs an HTTPS reverse proxy fronting the
`mcp-server` container on port 8080. The proxy must:

- terminate TLS on `scheduler.paynepew.dev` with an automatically renewing
  certificate;
- forward to `localhost:8080` (the `mcp-server` Docker container);
- compress responses;
- write access logs.

Two industry options for a single-VPS Linux deployment:

1. **nginx + certbot** — the industry default. Hand-rolled `nginx.conf` (typical
   30-50 lines for HTTPS + redirect + compression + logging) plus certbot
   running on cron for Let's Encrypt cert renewal every 60 days.
2. **Caddy 2** — newer single-binary proxy (Go, 2015 onwards). Bundles ACME
   client; HTTPS is the default. Configuration via Caddyfile (typically
   5-10 lines for our use case).

## Decision

**Caddy 2.**

The Caddyfile for the VPS is:

```caddyfile
scheduler.paynepew.dev {
    reverse_proxy localhost:8080
    encode gzip zstd
    log {
        output file /var/log/caddy/access.log
    }
}
```

Caddy obtains the Let's Encrypt cert on first start, renews it ~30 days before
expiry, and serves HTTPS / HTTP/2 / HTTP/3 with no further configuration. No
certbot, no renewal cron, no separate systemd unit.

## Alternatives considered

### nginx + certbot

- **Pros**: industry-standard skill, transfers directly to OpenResty / nginx-plus
  / many cloud load balancers; recruiters' ATS systems may search for "nginx"
  as a keyword; senior engineers universally recognise it.
- **Cons**: separate cert-renewal lifecycle (certbot cron) is an additional
  moving part with a documented incident class ("expired cert because renewal
  cron failed silently"); typical config is 4-6x longer than Caddy for the same
  outcome.

### Traefik

- Container-native, good for dynamic Docker discovery, but configuration is
  verbose (labels on every service, plus a `traefik.yml`). Designed for
  many-service environments — not a fit for one external endpoint.

### HAProxy

- HTTPS termination is rougher (separate `ssl-cert` directive + manual
  Let's Encrypt integration). Strength is L4 / TCP, not HTTPS-with-ACME.

## Trade-off acknowledged

nginx is the more transferable skill. Picking Caddy is a **deliberate trade**
of skill-transferability for operational simplicity, justified by:

1. **Cert-rotation surface area is zero with Caddy.** A single-VPS portfolio
   should not lose its demo URL to a missed certbot renewal three weeks before
   an interview.
2. **The Caddyfile is auditable in one screen.** A reviewer reading the project
   can verify the proxy configuration without context-switching.
3. **The choice itself is a positive signal when articulated.** "I evaluated
   nginx + certbot and chose Caddy because the cert-rotation incident class
   matters more than the keyword match" reads as engineering judgement, not
   trend-following.
4. **Cost of being wrong is one Terraform variable.** If a future workload
   needs nginx-specific features (slice / X-Accel / Lua modules) the proxy
   layer is the cheapest tier to swap.
5. **Emerging MCP plugin ecosystem.** Open-source projects YawLabs/caddy-mcp
   and lum8rjack/caddy-mcp expose Caddy itself as an MCP server, allowing
   LLM clients to dynamically inspect routes, check upstream health, and
   modify reverse-proxy state via MCP commands. nginx has no equivalent
   integration trajectory in 2026. While not in W3 scope, this positions
   the Caddy choice as forward-compatible with a future composability story
   where the task-scheduler MCP and the proxy MCP share a single LLM
   session (tracked as `(D-32)` in `.doc/learn/system-design.md` § 9.1).

## Consequences

- `Caddyfile` ships in the repo at `infra/vps/Caddyfile`.
- `bin/setup-vps.sh` (ADR-029 territory) installs Caddy via apt and points it
  at this file.
- Cert renewal is invisible — no cron, no certbot, no certs to monitor manually.
  Caddy logs renewal events to journald.
- The Fargate path (ADR-005, ALB + ACM) is unaffected — ALB handles TLS there.
  The VPS proxy and the ALB are independent deployment surfaces.
- The deciders will, when interviewed about reverse-proxy choice, lead with the
  cert-rotation reasoning above. Not "Caddy is newer" — that reads as
  trend-chasing without judgement.

## References

- Caddy 2 documentation: https://caddyserver.com/docs/
- Let's Encrypt ACME spec (RFC 8555): https://datatracker.ietf.org/doc/html/rfc8555
- "Caddy vs nginx" comparison threads (Hacker News, dev.to) — informal but
  the cert-rotation pain point is the most-cited Caddy advantage in practice
