# ADR-031: Replace UptimeRobot with Better Stack for VPS monitoring + public status page

- **Status**: Accepted
- **Date**: 2026-05-18
- **Deciders**: PaynePew
- **Source**: Direct decision during W3 implementation — UptimeRobot front-end inaccessible from the deciders' network (Taiwan); could not complete account creation / monitor configuration.
- **Related**: ADR-030 § B (this ADR supersedes the monitoring sub-decision only; backup-to-R2 and Fargate-validation sub-decisions remain in force).

## Context

ADR-030 § B selected UptimeRobot as the external uptime monitoring + public
status page provider for `https://scheduler.paynepew.dev/healthz`. The choice
was made on three properties:

1. Free tier covers the requirement (1 HTTPS monitor at 5-min interval,
   1 public status page, email + Slack webhook alerts).
2. Public status page is treated as a first-class portfolio artifact —
   interviewers clicking the demo URL can also see a 30/60/90-day uptime
   track record.
3. Externally observable, off-VPS — the monitor must survive the VPS being
   down, so self-hosted (Uptime Kuma) is structurally ruled out.

During W3-S06 (issue #65) implementation, the deciders discovered
`uptimerobot.com` is not reachable from their location. The website failed
to load across multiple sessions and connectivity tests — account creation,
monitor configuration, and status-page management are all gated behind this
front-end. Switching VPN / network paths is not an acceptable long-term
operational story for a portfolio artifact that needs to be administered
during interview prep.

The original property set (free, externally-hosted, status-page-as-artifact,
email + Slack alerts) remains the requirement. Only the vendor changes.

## Decision

Use **Better Stack** (formerly Better Uptime, betterstack.com/uptime) as
the monitoring + status page provider in place of UptimeRobot.

### Property comparison

| Requirement | UptimeRobot free | Better Stack free |
|---|---|---|
| HTTPS uptime monitor | 50 monitors / 5-min interval | 10 monitors / 3-min interval |
| Public status page | 1 (basic chrome) | 1 (polished, modern chrome) |
| Email alerts | ✅ | ✅ |
| Slack webhook alerts | ✅ | ✅ |
| Custom subdomain status page | paid tier | free tier (`<slug>.betteruptime.com`) |
| Custom domain on status page | paid tier | free tier (verified 2026-05-18 — pricing moved during the W3 sprint) |
| Reachable from deciders' network | ❌ (blocked) | ✅ |
| Cost | $0/mo | $0/mo |

The 3-min vs 5-min interval improves alert latency without changing the W3
acceptance gate (L5 still requires "≥ 24h continuous green" before close,
unchanged). The status-page quality differential is the secondary
portfolio-narrative reason — Better Stack's free-tier status page is more
polished, which matters when the page is linked from the resume.

### Configuration

- **Monitor**: HTTPS check `https://scheduler.paynepew.dev/healthz`, 3-min
  interval, alert when downtime persists ≥ 5 min (Better Stack free-tier
  ceiling; count-based thresholds not available on this plan — at 3-min
  poll cadence this is equivalent to ~2 consecutive failures).
- **Alert contacts**:
  - **Email** — deciders' personal email (built-in Better Stack channel).
  - **Slack** — Slack workspace OAuth app install, posting to
    `#scheduler-alerts`. The original "incoming webhook + `SLACK_WEBHOOK_URL`
    on the VPS, dual-purpose with the W4 daily ops digest" framing
    (inherited from ADR-030 §B) was abandoned during W3-S06 implementation
    in favour of the Slack-native OAuth flow Better Stack offers, which
    is operationally simpler. `SLACK_WEBHOOK_URL` provisioning on the VPS
    is therefore **deferred to W4** where the daily-digest consumer is
    actually built — no point storing an unused secret. The W4 daily
    digest will install its own incoming webhook independent of the
    Better Stack OAuth app; both can coexist in the same Slack workspace
    targeting the same channel.
- **Public status page**: served at **`https://status.paynepew.dev`** via
  Cloudflare CNAME → `statuspage.betteruptime.com` (managed in
  `terraform/cloudflare/main.tf` as `cloudflare_record.status`, ADR-031);
  title "ChatGPT Task Scheduler"; show 30/60/90-day uptime; linked from
  project README + `paynepew.dev` landing page. Custom domain was selected
  over the default `<slug>.betteruptime.com` subdomain because (a) Better
  Stack's free-tier custom-domain support materialised during the sprint
  (verified 2026-05-18) and (b) `status.paynepew.dev` lives under the same
  brand as `scheduler.paynepew.dev`, reinforcing the portfolio narrative.
  The CNAME is `proxied = false` so Better Stack's ACME issues a real
  Let's Encrypt cert against the target rather than Cloudflare replacing it.

### Cost

$0/mo. Better Stack free tier covers 10 monitors + 1 public status page
with email + Slack integrations. W3 uses 1 monitor + 1 status page; ample
headroom for the future R2-backup-age probe planned in ADR-030 § B
"Consequences".

## Consequences

- ADR-030 § B is partially superseded. The other sub-decisions in ADR-030
  (A: R2 backup, C: one-shot Fargate validation workflow, D: VPS hardening
  checklist) remain in force unchanged.
- `docs/PRD/deploy-w3.md` § D7-B is updated to reference Better Stack;
  L5 acceptance criteria substitutes "Better Stack status page ≥ 24h green"
  for the original UptimeRobot phrasing.
- The R2-backup-age probe mentioned in ADR-030's Consequences (weekly
  HTTP HEAD against newest R2 object, age > 48h triggers alert) is
  reassigned to Better Stack on the same free tier. Mechanism: Better
  Stack supports custom HTTP keyword checks on the response body, so a
  lightweight `bin/r2-backup-age.sh` running via GH Actions cron can post
  a status JSON to a public R2 object that Better Stack then keyword-checks
  for `"status":"fresh"`.
- The `git_sha` correlation comment in `app/config/settings.py` is updated
  to name Better Stack instead of UptimeRobot.
- Issue #65 (W3-S06) is retitled and rewritten to reference Better Stack;
  the status page URL in the acceptance criteria is
  `https://status.paynepew.dev` (not `<slug>.betteruptime.com`).
- `terraform/cloudflare/main.tf` gains `cloudflare_record.status`
  (CNAME `status` → `statuspage.betteruptime.com`, `proxied = false`,
  TTL 300 to match the scheduler A record). The Better Stack
  "1-click Cloudflare authorize" flow is deliberately bypassed — DNS
  state stays managed through `terraform plan` per the IaC discipline
  in commit `5ccff5d`.

## Known operational gap — deploy-time false alerts

`deploy-vps.yml` ends with `docker compose up -d --remove-orphans`, which
recreates the `mcp-server` container whenever `IMAGE_TAG` changes
(every push to `main`). The stop-old → start-new → Python boot + DB pool
warmup sequence yields a ~10–30 s window where Caddy returns 502; this
occasionally crosses two consecutive Better Stack polls and fires a false
"down" alert. Empirical: ~96.6 % monitor uptime in the first 2 h of
W3-S06 operation, with the noise cluster aligned to push timestamps.

The free-tier 5-min duration ceiling on the alert threshold cannot tune
the noise out — at 3-min poll cadence it is already ~2 consecutive
failures, which the deploy window does cross.

Resolution: tracked as **issue #77 (W3-S06b)** — pause the Better Stack
monitor via API before the SSH deploy step and resume it after the smoke
test (`if: always()` so a failed deploy doesn't silently mute the
monitor). Pause is preferred over scheduled-maintenance windows because
it is one PATCH each way and produces equivalent uptime accounting; MW
gives nicer status-page wording but is not load-bearing for the
portfolio narrative. The R2-backup-age probe planned above will need
the same pause/resume wrapper when it lands.

Out of scope for both this ADR and #77: rolling deploys with multiple
`mcp-server` replicas + Caddy load balance. Revisit if zero-downtime
becomes a portfolio requirement in W4+.

## References

- Better Stack free tier: https://betterstack.com/uptime
- Better Stack API (monitor pause/resume): https://betterstack.com/docs/uptime/api/monitors/
- ADR-030 (this ADR partially supersedes § B)
- Issue #65 — W3-S06 implementation slice
- Issue #77 — W3-S06b deploy-time monitor pause (known operational gap)
