# ADR-030: VPS operational concerns — Cloudflare R2 backup, UptimeRobot monitoring, one-shot Fargate validation workflow

- **Status**: Accepted (§ B partially superseded by [ADR-031](ADR-031-monitoring-better-stack-over-uptimerobot.md) on 2026-05-18 — vendor swap UptimeRobot → Better Stack; § A, § C, § D unchanged)
- **Date**: 2026-05-17
- **Deciders**: PaynePew
- **Source**: Grilling Session #4, Q-W3-7
- **Related**: ADR-027 (deployment target pivot to VPS), ADR-029 (VPS deployment mechanics), ADR-031 (monitoring vendor swap)

## Context

ADR-029 ships a VPS deployment pipeline but does not address three operational
concerns critical to a "runs for months without intervention" portfolio narrative:

1. **State durability**: VPS-local Postgres holds `jobs` / `job_runs` /
   `run_events` accumulated by the daily ops digest action (Session #5). If
   the Lightsail instance fails or a misconfigured `docker compose down -v`
   deletes the volume, all schedule state and run history is lost. The
   "scheduler has run X consecutive days" reliability story dies with it.
2. **Externally-observable uptime**: a portfolio URL going down silently is
   worse than going down loudly. A status page also doubles as a portfolio
   artifact ("Live uptime: 99.X%").
3. **Fargate path validation**: ADR-027 promised the Terraform / ECS / RDS /
   ALB architecture is real, not vapourware. This needs a workflow that proves
   it apply-able and destroy-able, and records evidence.

## Decision

Three sub-decisions, ship together as part of W3.

### A. Postgres backup → Cloudflare R2

Nightly cron on the VPS dumps Postgres and uploads to a Cloudflare R2 bucket.

**Mechanism** (`/etc/cron.daily/pg-backup.sh` deployed by `bin/setup-vps.sh`):

```bash
#!/usr/bin/env bash
set -euo pipefail
TS=$(date -u +%Y%m%d-%H%M%S)
cd /opt/chatgpt_task
docker compose exec -T postgres pg_dump -U postgres scheduler \
  | gzip > /tmp/scheduler-$TS.sql.gz
rclone copy /tmp/scheduler-$TS.sql.gz r2:chatgpt-task-backups/
rm /tmp/scheduler-$TS.sql.gz
rclone delete --min-age 7d r2:chatgpt-task-backups/
```

**Configuration**:

- R2 bucket `chatgpt-task-backups` in the deciders' Cloudflare account
- `rclone` config (`/root/.config/rclone/rclone.conf`) with R2 API token
  (token stored in VPS `.env`, read by `bin/setup-vps.sh` during provisioning)
- Retention: 7 daily snapshots; older snapshots deleted by the same script
- Schedule: nightly at 04:00 UTC (12:00 Taipei, low-traffic window)

**Cost**: $0/mo. R2 free tier: 10 GB storage + Class A operations (writes) 1
M/mo + Class B ops (reads) 10 M/mo. Daily snapshots are < 10 MB compressed for
portfolio-scale data; 7-day retention < 100 MB.

**Restore procedure** (documented in `docs/runbooks/restore-postgres.md`):

```bash
rclone copy r2:chatgpt-task-backups/scheduler-YYYYMMDD-HHMMSS.sql.gz /tmp/
gunzip -c /tmp/scheduler-*.sql.gz | docker compose exec -T postgres psql -U postgres scheduler
```

### B. UptimeRobot uptime monitoring + public status page

> **Superseded 2026-05-18 by [ADR-031](ADR-031-monitoring-better-stack-over-uptimerobot.md)**: vendor swapped to Better Stack (`uptimerobot.com` not reachable from the deciders' network). Property set and acceptance shape are unchanged; the text below is preserved for decision provenance. Read this section as "Better Stack" wherever it says "UptimeRobot", and "3-min interval" wherever it says "5-min interval".

- **Monitor**: HTTPS check against `https://scheduler.paynepew.dev/healthz`
  every 5 minutes (UptimeRobot free tier minimum interval)
- **Alerts**: email to the deciders' personal address + Slack webhook (the
  same webhook used by the daily ops digest, dual-purpose channel)
- **Public status page**: `https://stats.uptimerobot.com/<id>` linked from the
  project README and the `paynepew.dev` landing page
- **Cost**: $0/mo (UptimeRobot free tier: 50 monitors, 5-min interval, 1
  status page)

The status page is a deliberate portfolio artifact — interviewers who click
the demo URL can also see the 30-day uptime track record.

### C. One-shot Fargate validation GitHub Actions workflow

`.github/workflows/validate-fargate.yml`, manual `workflow_dispatch` trigger.

**Flow**:

1. Checkout + setup Terraform 1.7+
2. `terraform init` (state backend: S3 + DynamoDB lock, separate workflow run)
3. `terraform plan` → artifact uploaded for review
4. `terraform apply -auto-approve` (~10 min for VPC + RDS + ALB + ECS services)
5. Poll ALB target group until at least 1 healthy target (timeout 5 min)
6. Smoke test: `curl -fsS https://<alb-dns>/healthz` returns 200
7. **Evidence capture**:
   - `terraform output -json > artifacts/tf-outputs.json`
   - `aws ecs describe-services > artifacts/ecs-services.json`
   - `aws rds describe-db-instances > artifacts/rds.json`
   - upload as workflow artifact (90-day retention)
8. **Idle period**: `sleep ${inputs.duration_minutes:-30}` to allow manual
   inspection / Inspector flow / demo recording
9. `terraform destroy -auto-approve`
10. Final sanity: `aws ec2 describe-vpcs --filters Name=tag:Project,Values=chatgpt-task` returns empty list

**Trigger inputs**:

```yaml
on:
  workflow_dispatch:
    inputs:
      duration_minutes:
        description: 'Minutes to keep stack alive after smoke test'
        default: '30'
```

**AWS cost per run**: ~$0.50–$1.50 for 30 minutes of running (7 Fargate tasks +
RDS + ALB + minor data transfer). AWS Budgets alert at $10 / $30 is the safety
net.

**When this workflow actually runs**: once during the W4 demo recording week.
Not during W3 sprint, to avoid burning the validation budget before there's a
demo to record.

### D. VPS hardening checklist (final form, shipped via `bin/setup-vps.sh`)

| Item | Setting | Source of authority |
|---|---|---|
| SSH password auth | `PasswordAuthentication no` in `/etc/ssh/sshd_config` | OpenSSH defaults |
| SSH root login | `PermitRootLogin prohibit-password` | OpenSSH defaults |
| fail2ban | Default jail.local with SSH bantime 5m, maxretry 5 | Debian/Ubuntu standard |
| ufw rules | allow 22/tcp, 80/tcp, 443/tcp; deny incoming default | ufw docs |
| `unattended-upgrades` | security-only auto-update enabled | Ubuntu standard |
| Non-root user | `deploy` user owns `/opt/chatgpt_task`, in `docker` group | least privilege |
| Docker log driver | `local` driver, `max-size=10m`, `max-file=3` | Docker daemon.json |
| Swap | None (2 GB RAM plan has sufficient headroom) | Lightsail $5 plan |

Not adopted (deliberately, to keep ops surface lean):
- **Custom SSH port**: rejected as security-through-obscurity with no real
  defence-in-depth gain over key-only + fail2ban
- **Tailscale-only SSH**: would add a sign-in dependency for the deciders'
  own demo URL access; not worth the friction
- **CrowdSec / advanced IDS**: portfolio threat model doesn't justify

## Consequences

- `bin/setup-vps.sh` provisions everything in this ADR plus the docker compose
  stack from ADR-029 — single script, idempotent.
- README links to UptimeRobot status page; status page is treated as a
  portfolio artifact equal to GitHub stars.
- Backup script failure (e.g., R2 token rotated, rclone error) is invisible
  unless monitored. **Mitigation**: a follow-up monitor checks the age of the
  newest object in the R2 bucket weekly; if > 48 hours old, alert. Shipped as
  part of the UptimeRobot configuration: HTTP HEAD probe against an R2 public
  URL with custom age-check logic via a lightweight GH Actions cron.
- AWS Budgets alert at $10 (warn) and $30 (cap) is mandatory before the
  `validate-fargate.yml` workflow is ever dispatched. Documented in
  `docs/runbooks/pre-fargate-validation-checklist.md`.

## References

- Cloudflare R2 pricing & free tier: https://developers.cloudflare.com/r2/pricing/
- UptimeRobot free tier: https://uptimerobot.com/pricing/
- `rclone` R2 setup: https://rclone.org/s3/#cloudflare-r2
- ADR-029 (deployment mechanics — this ADR extends operational surface)
- ADR-027 (deployment target pivot — this ADR delivers on the Fargate
  validation promise)
