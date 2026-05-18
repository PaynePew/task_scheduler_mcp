# PRD: ChatGPT Task Scheduler — W3 Deployment Surface

> **Scope**: AWS deployment surface for the W1+W2 scheduler — Terraform IaC for the Fargate path (preserved as a design artifact), VPS-first runtime on AWS Lightsail Tokyo, CI/CD pipeline, operational ground (backup + monitoring), and a 7-layer acceptance gate. **Not** in scope: Action sprint (Slack + GitHub API integrations, W4), demo video (W4), structured logging / CloudWatch dashboards (W4), ALB OIDC integration (W4 D-X).
>
> **Deliverable**: A running scheduler at `https://scheduler.paynepew.dev` (~$5/mo), reproducible via `bin/setup-vps.sh`, deployed by GitHub Actions on every push to `main`. A Fargate Terraform module set validated end-to-end (apply→smoke→destroy) once during W4 demo recording.
>
> **Status**: Design 100% locked. Source decisions in `.doc/session/grilling-state.md` (Q-W3-1 to Q-W3-8) + ADR-024 through ADR-030 + this PRD.
>
> **Generated**: 2026-05-17 via `/to-prd` after Grilling Session #4.
>
> **Supersedes**: W2 PRD's "Out of scope for W2, in scope for W3" section. The β-cut server-side LLM (ADR-018) removed the original P3 topology motivation; the VPS-first pivot (ADR-027) replaces always-on Fargate as the runtime target.

---

## Problem Statement

After W1 + W2 shipped a working scheduler (cron + chaining + MCP resources + prompts, all running on `docker compose --profile full`), the project has no externally-reachable demo. Reviewers must clone and run locally — not an option for portfolio narrative or interview validation.

The project needs:

1. **A live URL** that interviewers can hit to validate the scheduler is real.
2. **Reproducible deployment** — not a hand-rolled "I SSH'd in and ran commands"; a code-defined recreate path verifiable by an auditor.
3. **An AWS architectural story** — the resume positioning is Backend/Infra Engineer; the Terraform / ECS / RDS / ALB design is the "language" the role expects.
4. **Cost discipline** — deciders are job-searching with no income; ~$120/mo for an always-on cloud demo is not viable.
5. **A reliability story** — "scheduler running N days uninterrupted" requires durable state + monitoring + backup.
6. **A W3-done definition** — what does shipping look like, given W1 had a 6-step Inspector flow and W2 had a layered gate?

The available solution space spans: full cloud (cost prohibitive), full local (no live URL), and various hybrids.

---

## Solution

A two-target deployment strategy where the runtime and the design artifact are explicitly separated.

1. **Runtime target = AWS Lightsail Tokyo, $5/mo (2 GB RAM, 1 vCPU, 60 GB SSD).** Reachable at `https://scheduler.paynepew.dev`. Provisioned by an idempotent `bin/setup-vps.sh` script. Daily Postgres backup to Cloudflare R2. Better Stack monitors the live URL with a public status page (ADR-031, swapped from UptimeRobot when its front-end was unreachable from the deciders' network).

2. **Design target = AWS ECS Fargate / RDS / ALB / SQS / IAM / VPC, fully Terraform-coded.** Validated end-to-end by a `workflow_dispatch` GitHub Actions workflow (`validate-fargate.yml`) that applies → smoke-tests → captures evidence → destroys. Runs once during W4 demo recording (bill < $5 per run).

3. **CI/CD = GitHub Actions on every push to `main`.** Build container image, push to `ghcr.io/paynepew/chatgpt_task` tagged with `latest` + `${git_sha}`. SSH into the VPS, pull the image, run migrations, recreate the docker-compose stack, smoke test against the live URL.

4. **Reverse proxy + HTTPS = Caddy 2.** Single-binary auto-ACME, 5-line Caddyfile. Chosen for cert-rotation simplicity, audit-ability, and forward-compatibility with the emerging Caddy MCP plugin ecosystem.

5. **VPS hardening = `bin/setup-vps.sh` ships SSH key-only auth, fail2ban, ufw, `unattended-upgrades`, non-root `deploy` user, Docker log driver limits.**

6. **7-layer acceptance gate** spans code-green (L1), VPS provision (L2), live URL (L3), functional smoke including chain test (L4a + L4b), operational gates (L5), Fargate validation (L6, W4), and demo video (L7, W4).

The compose footprint on the VPS mirrors local development — Postgres + ElasticMQ as containers, not managed services. Zero "works on my machine, breaks on RDS" risk.

---

## User Stories

### Live deployment & demo URL

1. As an interviewer, I want a URL on the candidate's resume that returns HTTP 200 with a valid TLS cert, so I can verify the project is real without cloning.
2. As an interviewer, I want the URL to respond within ~1 s, so the demo doesn't feel broken.
3. As a recruiter performing a spontaneous LinkedIn click-through, I want the demo URL to be live without prior notification.
4. As the deciders, I want the live URL on `scheduler.paynepew.dev` so the brand is consistent across portfolio projects (e.g. future `qr.paynepew.dev`).
5. As a reviewer, I want a public uptime status page linked from the README, so the reliability narrative is verifiable, not just asserted.

### Reproducible VPS provisioning

6. As the deciders, I want to provision a fresh Lightsail instance from blank to fully-running with one script, so the recreate path is real, not heroic memory.
7. As an auditor, I want the entire VPS setup visible in a single ~50-line bash script, so the security posture is reviewable in one screen.
8. As a future operator, I want the script to be idempotent — running it on an already-provisioned VPS should be a safe no-op.

### CI/CD pipeline

9. As the deciders, I want every push to `main` to reach the live VPS within ~5 minutes without manual intervention.
10. As the deciders, I want failed migrations to fail the deploy, not silently roll forward.
11. As the deciders, I want a smoke test (`curl /healthz`) after every deploy, with the workflow failing on non-200.
12. As the deciders, I want to roll back to any prior commit by changing one variable (`IMAGE_TAG`) on the VPS and re-running `docker compose up -d`.
13. As an auditor, I want the deploy workflow YAML to be ~30 lines and use only well-known actions (`docker/build-push-action`, `appleboy/ssh-action`).

### Fargate Terraform validation

14. As an interviewer, I want to see proof the candidate's AWS architecture is real, not handwritten in markdown — a workflow that actually applied and destroyed it end-to-end.
15. As the deciders, I want the validation workflow to be `workflow_dispatch` (manual) so it doesn't burn AWS cost on every push.
16. As the deciders, I want the workflow to capture Terraform outputs, ECS service descriptions, and RDS status as artifacts, so the evidence persists for 90 days.
17. As the deciders, I want a `duration_minutes` input on the workflow so I can keep the stack alive for 30 minutes (or 60, etc.) to record a demo video, then auto-destroy.
18. As the deciders, I want AWS Budgets alerts at $10 (warn) and $30 (cap) as a safety net against the validation workflow running away.

### Caddy reverse proxy + HTTPS

19. As the deciders, I want HTTPS active on `scheduler.paynepew.dev` without manually running certbot.
20. As the deciders, I want the TLS cert to renew automatically without my intervention.
21. As the deciders, I want the Caddyfile to be in the repo (version-controlled) so changes go through PR review.
22. As an interviewer, I want the reverse-proxy choice (Caddy vs nginx) to be justified by an ADR, so I can evaluate engineering judgement.

### State durability & monitoring

23. As the deciders, I want nightly Postgres backups stored off-VPS (Cloudflare R2), so VPS loss doesn't equal data loss.
24. As the deciders, I want a 7-day retention policy so old backups self-clean.
25. As the deciders, I want to be alerted if the live URL goes down, via email + Slack.
26. As an interviewer, I want a public 30-day uptime track record linkable from the README, so reliability is observable.
27. As the deciders, I want a documented restore procedure (`docs/runbooks/restore-postgres.md`), so the backup is verifiable, not theoretical.

### Acceptance gate

28. As the deciders, I want a clear "W3 done" definition with concrete pass criteria, so the sprint can close.
29. As a reviewer, I want the functional smoke test (L4) to cover both the cron path (L4a: echo recurring) and the chain path (L4b: A→B chained), so "everything wired up" proof is comprehensive.
30. As the deciders, I want the Fargate validation (L6) and demo video (L7) explicitly deferred to W4, so W3 sprint can ship without scope creep.

### Cost discipline

31. As the deciders, I want the monthly fixed cost capped at ~$5 USD, so the deployment runs through indefinite job search without financial strain.
32. As the deciders, I want the one-shot Fargate validation cost capped at < $5 per run, with AWS Budgets as the safety net.

### Forward compatibility

33. As the deciders, I want the VPS deployment architecture to NOT preclude future migration to Fargate — same `docker-compose.yml` shape, same secrets pattern, same migration story.
34. As the deciders, I want the Caddy choice to be forward-compatible with the emerging Caddy MCP plugin ecosystem (`(D-32)`), so the proxy isn't a dead-end if MCP composability becomes a project direction.
35. As the deciders, I want the Action Sprint scope (W4: Slack + GitHub API integrations) to land cleanly on top of the W3 deployment surface, without infrastructure rework.

---

## Implementation Decisions

### D1. W3 tier scoping (ADR-024)

Three-tier scope with explicit cuts:

- **Tier 1 (must ship)**: Terraform module set, network topology decision, connection-pool reconciliation, GH Actions CI/CD, AWS Budgets + IAM least-privilege, HTTPS via ACM (Fargate path) / Caddy auto-ACME (VPS path), Alembic migration on deploy.
- **Tier 2 (should ship)**: `job_runs` declarative `PARTITION BY RANGE`, RDS Multi-AZ posture (decided: off in W3), Secrets Manager (Fargate path) / `.env` 0600 (VPS path), post-deploy smoke test.
- **Tier 3 (deferred)**: ElastiCache cut entirely (no consumer post-ADR-008), ALB OIDC deferred to W4 D-X, `pg_partman` automation deferred to W4, CloudWatch dashboards / structured logging deferred to W4 (consistent with W2 PRD), multi-region / read replica / WAF remain in `(D-2)`/`(D-3)`/`(D-23)` backlog.

### D2. Network topology (ADR-025) — Fargate design target

P1 chosen: public ECS tasks + private RDS + 2 AZ + S3 Gateway Endpoint, no NAT Gateway.

Rationale: ADR-018's β-cut removed the original P3 motivation (Bedrock VPC Endpoint); `http_call` action structurally requires arbitrary internet egress (P3 incompatible). Between P1 and P2, the security gain of P2 (NAT-fronted private tasks) is marginal — both have the ALB as the effective inbound attack surface — but P2 costs $32-64/mo. P1 is the cost-aware judgement.

VPC layout:

```
VPC 10.0.0.0/16
├── public  10.0.0.0/24   (ap-northeast-1a)   ALB + ECS tasks
├── public  10.0.1.0/24   (ap-northeast-1c)   ALB + ECS tasks
├── private 10.0.10.0/24  (ap-northeast-1a)   RDS primary
└── private 10.0.11.0/24  (ap-northeast-1c)   RDS standby slot (Multi-AZ off in W3)
```

SG layering: `alb-sg` (443 from `0.0.0.0/0`) → `ecs-tasks-sg` (8080 from `alb-sg`) → `rds-sg` (5432 from `ecs-tasks-sg`).

S3 Gateway Endpoint: enabled (free); routes ECR image-layer pulls off public IGW.

### D3. ECS service topology (ADR-026) — Fargate design target

Five long-running services, no consolidation. Replicas: `mcp-server` N=2, `watcher` N=2, `worker` N=1 (autoscale to 4 on SQS `ApproximateNumberOfMessagesVisible`), `recurring_watcher` N=1, `chain_watcher` N=1. `migrate` as one-shot task before each deploy.

Idle footprint: 7 tasks. Peak: 10. Autoscaling enabled only on `worker` — `mcp-server` autoscaling on ALB request count rejected as portfolio traffic profile is flat-zero; `recurring_watcher` / `chain_watcher` not autoscaled because event-reactor load is bounded by cursor consumption.

### D4. Deployment target pivot to VPS-first (ADR-027) — runtime decision

AWS Lightsail Tokyo ($5/mo, 2 GB RAM / 1 vCPU / 60 GB SSD / 3 TB transfer) chosen as the runtime target.

The ECS Fargate Terraform path (D2 + D3) is preserved as a **design artifact** validated end-to-end once via `validate-fargate.yml` (D7) during W4 demo recording.

Rationale:

- Fargate idle cost $117-145/mo is not feasible for job-searching deciders
- Lightsail Tokyo gives 30-50 ms Taiwan latency (matches the deciders' interview market)
- AWS brand preserved (Lightsail is AWS-managed VPS)
- $5 plan's 2 GB RAM provides comfortable headroom over Postgres + 5 Python services + OS
- `terraform apply` reconstitutes the full Fargate topology in ~10 minutes; the design is verifiable, not hypothetical

Rejected alternatives: Vultr / Linode Tokyo (1 GB tight + lose AWS brand); Hetzner Germany (250-280 ms latency to Taiwan); Oracle Free Tier (instances killed unpredictably); Contabo Singapore (Singapore latency, no brand recognition).

Apex domain: `paynepew.dev` (Cloudflare Registrar, ~$10/yr). Scheduler at `scheduler.paynepew.dev`. Cloudflare DNS (free) for resolution.

### D5. Reverse proxy + HTTPS = Caddy 2 (ADR-028)

Caddy 2 on the VPS terminates TLS for `scheduler.paynepew.dev`. Caddyfile is 5 lines:

```
scheduler.paynepew.dev {
    reverse_proxy localhost:8080
    encode gzip zstd
    log {
        output file /var/log/caddy/access.log
    }
}
```

Caddy obtains the Let's Encrypt cert on first start and auto-renews — no certbot, no cron, no separate systemd unit.

Trade-off acknowledged: nginx has higher ATS keyword value. Caddy chosen for: cert-rotation incident class elimination, 5-line auditable config, **and** the emerging Caddy MCP plugin ecosystem (`YawLabs/caddy-mcp`, `lum8rjack/caddy-mcp`) that positions the proxy as forward-compatible with `(D-32)` composability backlog.

The Fargate design path uses ALB + ACM (ADR-005); the two TLS surfaces are independent.

### D6. VPS deployment mechanics (ADR-029)

**Build location**: GitHub Actions runners (free for public repo). **Image transport**: `ghcr.io/paynepew/chatgpt_task` (free for public packages). **Image tags**: `latest` + `${git_sha}` (no semver — portfolio code has no release ceremony). **VPS action on deploy**: SSH into VPS, `docker compose pull`, `docker compose run --rm migrate`, `docker compose up -d --remove-orphans`, smoke test `curl https://scheduler.paynepew.dev/healthz`.

VPS-side `docker-compose.yml` references `image: ghcr.io/paynepew/chatgpt_task:${IMAGE_TAG:-latest}` for the 5 services. Postgres 16 + ElasticMQ + Caddy all containerised. **Same compose shape as local development** (only `build:` swapped for `image:`).

Rationale for Postgres-in-container on VPS (vs managed RDS):

- $0 extra cost vs RDS $12/mo
- Identical to local dev mental model
- Daily R2 backup covers data durability (D7-A)
- Migration to RDS (Fargate path) is a single environment-variable swap

Secrets policy:

- VPS-only secrets (DB password, future GitHub PAT, future Slack webhook URL) live in `/opt/chatgpt_task/.env`, chmod 0600, owner `deploy:deploy`
- GitHub Actions only holds `VPS_SSH_KEY` and uses auto-injected `GITHUB_TOKEN` for ghcr.io
- Nothing in `.env` enters git (gated by `.gitignore`)

Rollback procedure: `IMAGE_TAG=<prev_sha> docker compose up -d` on the VPS. Prior images stay in `ghcr.io` storage indefinitely (free for public packages).

### D7. Operational concerns (ADR-030)

**A. Postgres backup → Cloudflare R2 (free tier)**

Nightly cron on the VPS:

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

Cloudflare R2 chosen over S3 / B2 for: zero egress fee, free 10 GB tier, S3-compatible API (frictionless AWS migration). Daily snapshots < 10 MB compressed; 7-day retention < 100 MB — well within free tier.

Restore procedure (`docs/runbooks/restore-postgres.md`):

```bash
rclone copy r2:chatgpt-task-backups/scheduler-YYYYMMDD-HHMMSS.sql.gz /tmp/
gunzip -c /tmp/scheduler-*.sql.gz | docker compose exec -T postgres psql -U postgres scheduler
```

**B. Better Stack uptime monitoring** (ADR-031 — swapped from UptimeRobot)

HTTPS check `https://scheduler.paynepew.dev/healthz` every 3 min (Better Stack free-tier minimum, tighter than UptimeRobot's 5-min). Alerts: email + Slack webhook (same webhook used by future daily ops digest). **Public status page at `https://status.paynepew.dev`** (Cloudflare CNAME → `statuspage.betteruptime.com`, managed in `terraform/cloudflare/main.tf`; Better Stack free-tier custom-domain support became available during the sprint — verified 2026-05-18) linked from README — treated as first-class portfolio artifact equivalent to GitHub stars.

**Deploy-time noise suppression (issue #77).** `deploy-vps.yml` wraps the SSH deploy + smoke test in two Better Stack API calls — PATCH `{"paused": true}` before, PATCH `{"paused": false}` after with `if: always()` so a failed deploy cannot leave the monitor silently muted. Eliminates the 10–30 s `mcp-server` recreate gap from uptime accounting without paying for rolling deploys.

**C. One-shot Fargate validation workflow** (`.github/workflows/validate-fargate.yml`)

`workflow_dispatch` trigger with `duration_minutes` input. Flow: init → plan → apply → poll ALB target healthy → smoke `/healthz` → capture evidence artifacts (Terraform outputs + ECS describe-services + RDS describe-db-instances + ALB DNS screenshot) → sleep `duration_minutes` → destroy → final sanity check (`describe-vpcs --filters Name=tag:Project,Values=chatgpt-task` returns empty).

Estimated per-run cost: $0.50-1.50. Pre-flight checklist in `docs/runbooks/pre-fargate-validation-checklist.md`: Budgets alert confirmed at $10 / $30, IAM keys ready, DNS swap plan if validation includes external smoke test.

**D. VPS hardening shipped via `bin/setup-vps.sh`**:

- SSH password auth off, root login key-only
- `fail2ban` with default SSH jail (5-min ban after 5 failed attempts)
- `ufw` allows 22 / 80 / 443 only
- `unattended-upgrades` security-only auto-update
- Non-root `deploy` user in `docker` group
- Docker daemon log driver `local`, `max-size=10m`, `max-file=3`
- No swap (2 GB plan sufficient)

Rejected hardening additions: custom SSH port (security-through-obscurity), Tailscale-only access (friction overhead), CrowdSec / advanced IDS (over-engineered for portfolio).

### D8. Acceptance gate layers (Q-W3-8)

7 layers, L1-L5 in W3 sprint, L6-L7 in W4:

- **L1 Code green**: all GH Actions workflows green on `main`; `terraform plan` succeeds in PR
- **L2 Local provision**: `bin/setup-vps.sh` on a fresh Lightsail instance → all services healthy, no manual patches
- **L3 Live URL**: `https://scheduler.paynepew.dev/healthz` returns 200 from external network; TLS valid; response < 1 s
- **L4 Functional smoke** (both sub-gates required):
  - **L4a** Echo recurring (`* * * * *`) → 5 min later `task.list.v1` shows ≥ 2 completed JobRuns → proves `mcp-server` + `watcher` + `worker` + `recurring_watcher` alive and inter-communicating.
  - **L4b** Chain A→B: create `echo` job A (immediate); create `echo` job B with `trigger_on_job_id=A, trigger_on_status=SUCCEEDED`; wait 30s; assert both completed → proves `chain_watcher` alive. Cleanup: `task.cancel.v1` both job IDs.
- **L5 Operational gates**: Better Stack status page green 24h+; R2 has first nightly pg_dump; manual restore drill against fresh local Compose passes.
- **L6 Fargate evidence** (W4): `validate-fargate.yml` runs end-to-end successfully; artifacts captured; bill < $5 per run.
- **L7 Demo video** (W4): 3-minute portfolio demo.

### D9. Module surface

| Module | Status | Purpose |
|---|---|---|
| `terraform/` (sub-modules: vpc, iam, rds, ecs, alb, sqs, ecr, logs) | New | Fargate design artifact, validated by L6 |
| `terraform/cloudflare/` | New | DNS A record for `scheduler.paynepew.dev` → Lightsail static IP; CNAME for `status.paynepew.dev` → Better Stack edge (ADR-031) |
| `bin/setup-vps.sh` | New | Idempotent VPS provisioning (Docker, Caddy, ufw, fail2ban, deploy user, systemd unit, R2 rclone config, pg-backup cron) |
| `infra/vps/Caddyfile` | New | Reverse proxy config |
| `infra/vps/docker-compose.yml` | New | VPS-flavored Compose (`image:` refs to ghcr.io, no `build:`) |
| `infra/vps/.env.example` | New | Secret slot template |
| `.github/workflows/deploy-vps.yml` | New | Build → ghcr.io → SSH pull deploy on every push to `main` |
| `.github/workflows/validate-fargate.yml` | New | Manual `workflow_dispatch` Fargate apply / smoke / destroy validation |
| `.github/workflows/terraform-ci.yml` | New | `terraform fmt -check && terraform validate && tflint` on PRs touching `terraform/` |
| `app/mcp/healthz.py` (or similar) | New (small) | `/healthz` endpoint on `mcp-server` |
| `docs/runbooks/restore-postgres.md` | New | R2 pull → local restore procedure |
| `docs/runbooks/pre-fargate-validation-checklist.md` | New | Budgets / IAM / DNS readiness before invoking `validate-fargate.yml` |
| `docs/W3-VERIFICATION.md` | New | L2 / L3 / L4 manual click-through (parallels W2-VERIFICATION.md) |
| `README.md` | Modify | Demo URL section, Better Stack status page link, deployment architecture overview |

No application-layer code changes outside the new `/healthz` handler.

---

## Testing Decisions

### What makes a good test (here, extending W2)

- **Test the deployment behaviour, not the Terraform syntax**: `terraform plan` lint in CI is sufficient for syntax; the meaningful test is L6 (`validate-fargate.yml` apply→smoke→destroy round-trip).
- **Test the live URL, not the local container**: VPS smoke tests via `curl https://scheduler.paynepew.dev/healthz` from a GitHub Actions runner — external network path matters.
- **Don't mock the database in the L4 smoke**: the L4 test creates real `Job` rows via `task.create.v1`, waits for the real watcher tick + worker dispatch, asserts on real `JobRun` rows. The mutation patterns under deployment (real `FOR UPDATE SKIP LOCKED` contention, real `processed_by` JSONB merge) only exhibit production semantics on the real stack.
- **Don't sleep for real cron tick where not needed**: L4a uses `* * * * *` (1-min cron) so the wait is bounded at ~3 min, not the 5-min Watcher polling window.
- **Restore drill is part of acceptance**: L5 includes a manual restore-from-R2 drill against a local Compose instance. Tests the documented runbook, not just the backup script's exit code.

### Test surface by module

| Module | Test type | What's covered |
|---|---|---|
| `terraform/` modules | CI: `terraform fmt`, `terraform validate`, `tflint` in PR workflow | Syntax + sanity, not deployment correctness |
| `terraform/` (full apply) | One-shot integration: `validate-fargate.yml` (L6 in W4) | End-to-end apply → smoke → destroy; AWS-side verifiable |
| `bin/setup-vps.sh` | Manual integration: provision a fresh Lightsail instance (L2 in W3) | Idempotency + completeness of provisioning |
| `infra/vps/docker-compose.yml` | Manual integration: `docker compose up -d` on VPS + L4 smoke | Container orchestration on the VPS |
| `.github/workflows/deploy-vps.yml` | Live integration: every push to `main` is a test | Build, image push, SSH deploy, migration, smoke test |
| `.github/workflows/validate-fargate.yml` | Manual W4 invocation | Forward + reverse path of Terraform |
| `/healthz` endpoint | Unit + integration | Returns 200 with DB connectivity check |
| Backup cron + R2 upload | Integration (L5 part of W3 acceptance) | Backup file appears in R2 within 24h of provisioning |
| Restore runbook | Manual drill (L5 part of W3 acceptance) | Pulled backup restores cleanly into a fresh Compose schema |

### Coverage target

Application code coverage from W1+W2 (80%+) maintained — W3 does not touch most app code. New deployment scripts are tested via the acceptance gate (L1-L5) rather than unit tests.

### Prior art

- W2 `docs/W2-VERIFICATION.md` — pattern for manual click-through verification.
- W1+W2 `tests/integration/test_e2e_inspector_flow.py` — in-process MCP testing pattern; L4a + L4b can extend this with an `against=live_url` variant (TBD whether this becomes part of CI or stays manual).
- Industry references for the apply-validate-destroy CI pattern: HashiCorp Terratest examples; many open-source IaC repos use `workflow_dispatch` for ephemeral validation.

---

## Out of Scope

These belong to later weeks (W4) or the future-upgrade list:

### Out of scope for W3, in scope for W4

- **Action Sprint** — Slack webhook integration (`slack_post` action), GitHub API integration (`github_digest` action), 1 mocked third-party action with full schema/ADR, ADRs ADR-031+ for action handlers, daily ops digest as a real recurring workflow on the VPS.
- **L6 Fargate validation execution** — the workflow ships in W3, but is invoked once during W4 demo recording week.
- **L7 Demo video** — 3-minute portfolio demo recorded against both the VPS (daily digest in flight) and a temporarily-applied Fargate stack.
- **CloudWatch dashboards / structured JSON logging** — already W4 per W2 PRD; reaffirmed.
- **README polish + architecture diagram + blog post** — W4.
- **ALB OIDC integration replacing `user_id` resolver step 2** — deferred to W4 D-X (see ADR-024 rationale).
- **Localised UI / prompts** — W4 polish.

### Out of scope for W3, in scope for W4 "行有餘力" backlog (D-X)

- `(D-32)` Caddy MCP plugin integration for multi-MCP composability — see `.doc/learn/system-design.md` § 9.1.
- `pg_partman` automation cron — partition structure ships in W3 (Tier 2.1), automation deferred.
- Multi-AZ RDS posture — Terraform variable defaults to `off` in W3; flipping to `on` is a one-line change W4+.
- RDS Proxy — connection-pool reconciliation in W3 stays at "documented for AWS, irrelevant for VPS"; RDS Proxy adoption is W4+ decision when actual AWS workload runs.
- Multi-environment (dev / staging / prod) — one environment is sufficient for portfolio.
- WAF on ALB — W4+.

### Permanently out of scope

- Multi-region active-active.
- EKS / Kubernetes.
- Lambda-only architecture (covered by `(D-1)` blog post comparison).
- Server-side LLM action (`llm_summarize`, `llm_chat`) — cut in W2 (ADR-018), revisitable as W4 D-X.

---

## Further Notes

### Companion documents (must-read for implementers)

- **`.doc/session/grilling-state.md`** — decision ledger Q-W3-1 to Q-W3-8 + W2 prior decisions.
- **`docs/PRD/prototype-w1.md`** + **`docs/PRD/bonus-w2.md`** — the surface this W3 implementation deploys.
- **`CONTEXT.md`** — domain glossary; W3 introduces no new domain terms.
- **`.doc/learn/system-design.md`** § 8 (4-week plan) and § 9.1 (`(D-32)` future composability).
- **`.doc/learn/aws-vpc-networking.md`** — networking deep-dive written during Grilling Session #4 (Q-W3-2).
- **`docs/W3-VERIFICATION.md`** — manual verification flow (created as part of W3 acceptance gate).
- **`docs/runbooks/restore-postgres.md`** + **`docs/runbooks/pre-fargate-validation-checklist.md`** — created during W3 implementation.

### ADRs grounding this PRD

| ADR | Title | Captures |
|---|---|---|
| ADR-024 | W3 tier scoping | What ships / what's cut (ElastiCache, OIDC, `pg_partman` automation) |
| ADR-025 | Network topology | P1 public ECS + private RDS, 2 AZ, no NAT, S3 Gateway Endpoint |
| ADR-026 | ECS service topology | 5 long-running services, fixed replicas, worker autoscaling |
| ADR-027 | Deployment target pivot | VPS-first (Lightsail Tokyo), Fargate as design artifact |
| ADR-028 | Caddy 2 over nginx | Cert rotation + MCP plugin ecosystem forward-compat |
| ADR-029 | VPS deployment mechanics | Build on GH Actions, push ghcr.io, SSH pull, containerised Postgres+ElasticMQ |
| ADR-030 | Operational concerns | R2 backup, monitoring (§ B partially superseded by ADR-031), one-shot Fargate validation workflow |
| ADR-031 | Monitoring vendor swap | Better Stack replaces UptimeRobot (front-end inaccessible from deciders' network) |

### Verification (echoes Q-W3-8)

W3 is "done" when L1-L5 pass:

1. **L1**: `gh run list --branch main --workflow=deploy-vps.yml --json conclusion` shows `success`; `terraform fmt -check && terraform validate` passes in PR.
2. **L2**: A fresh Lightsail instance provisioned by `bin/setup-vps.sh` reaches `docker compose ps` showing all services `running (healthy)` without manual intervention.
3. **L3**: `curl -fsS https://scheduler.paynepew.dev/healthz` returns 200 from a GH Actions runner (external network proof); `openssl s_client -connect scheduler.paynepew.dev:443` shows a valid Let's Encrypt cert.
4. **L4a** + **L4b**: documented in `docs/W3-VERIFICATION.md`; pass criteria per D8.
5. **L5**: Better Stack status page shows ≥ 24h of green; R2 bucket contains the most recent nightly snapshot; restore drill against a fresh local Compose succeeds.

L6 + L7 are W4 deliverables and explicitly excluded from W3 "done".

### Risks and mitigations

- **Risk: Lightsail Tokyo experiences regional outage.** Mitigation: documented as a known portfolio-tier limitation in `README.md`; recovery requires `bin/setup-vps.sh` on a new instance + R2 backup restore. Estimated RTO ~1 hour.
- **Risk: R2 token rotates silently; backups stop without alarm.** Mitigation: Better Stack keyword check against a public R2 status object refreshed weekly by a GH Actions cron (`bin/r2-backup-age.sh` → posts `{"status":"fresh"}` if newest snapshot < 48h, else `"stale"`); Better Stack alerts on the keyword absence (planned during W3 monitoring setup).
- **Risk: Caddy MCP plugin landscape destabilises; D-32 forward-compat narrative weakens.** Mitigation: ADR-028's primary reasoning (cert rotation) stands independent; MCP plugin reasoning is supporting, not load-bearing.
- **Risk: GitHub Actions free-tier exhaustion** (2000 min/mo on private repos). Mitigation: repo is public; GH Actions on public repos is free. Build cache via `cache-from/to: type=gha` keeps each run < 3 min.
- **Risk: VPS `.env` file leaked via misconfigured docker-compose.** Mitigation: chmod 0600, owner `deploy`, never `git add`; `.env.example` ships with empty placeholders.
- **Risk: deciders forget to invoke `validate-fargate.yml` before W4 demo recording.** Mitigation: `docs/runbooks/pre-fargate-validation-checklist.md` documents pre-flight; mentioned in `README.md` "Roadmap" section.

### Decision provenance

Every decision in this PRD traces to:

- A `Q-W3-#` entry in `.doc/session/grilling-state.md` (Session #4 decision log), or
- An ADR in `docs/adr/ADR-024` through `ADR-030`, or
- A W1 / W2 PRD section that W3 extends.

If a future implementer disagrees with any decision, that's the audit trail to revisit.

### Cost projection

| Phase | Monthly cost (USD) |
|---|---|
| W3 idle (Lightsail $5 + R2 free + Better Stack free + Cloudflare DNS free + domain prorated ~$1) | **~$6** |
| W3 active deploys (GH Actions free on public repo + image pushes free) | $0 incremental |
| W4 one-shot Fargate validation | < $5 per run, one-time |
| 12-month projected (job search active) | **~$70-80** |
| Comparison: original always-on Fargate plan | $1400-1700 |

---

*Generated 2026-05-17 from `.doc/session/grilling-state.md` (Q-W3-1 to Q-W3-8) + ADR-024 through ADR-030 + W1 / W2 PRDs via `/to-prd` after Grilling Session #4.*
