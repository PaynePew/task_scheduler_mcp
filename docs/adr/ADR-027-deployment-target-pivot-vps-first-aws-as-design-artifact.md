# ADR-027: Deployment target pivot — VPS first, AWS architecture preserved as design artifact

- **Status**: Accepted
- **Date**: 2026-05-17
- **Deciders**: PaynePew
- **Source**: Grilling Session #4, Q-W3-4b cost-posture follow-up
- **Related**: ADR-004 (ECS Fargate target), ADR-005 (ALB), ADR-024 (W3 tier scoping), ADR-025 (network topology), ADR-026 (service topology)

## Context

ADR-024 through ADR-026 locked the W3 deliverable as a Terraform-driven AWS
lift-and-shift to ECS Fargate / RDS / ALB / SQS, with anticipated idle cost in
the range of **$117-145/mo** (ADR-024 cost recalculation, with Public IPv4
surcharge from 2024-02 included).

During Grilling Session #4 follow-up the project's financial constraint
surfaced explicitly: the deciders are job-searching with no current income,
and **a $95-120/mo recurring cost is not affordable**. Continuing the original
plan would force one of:

- accept the recurring cost (not feasible);
- defer W3 indefinitely (loses portfolio momentum + leaves ADR-024/025/026
  stranded as design without verification);
- adopt pause/resume operational complexity (Q-W3-4b explored this — JS-3
  wake-on-demand brought monthly cost down to ~$25-40 but introduced 90-second
  cold-start friction and lost the always-on benefit needed when a daily ops
  digest action lands in the Action Sprint).

A fourth option emerged: **separate "designed for" from "deployed to"**.

## Decision

**Deployment target = AWS Lightsail Tokyo, $5/mo (2 GB RAM, 1 vCPU, 60 GB SSD).
AWS architecture preserved as Terraform code + ADR series, validated by a
one-time `terraform apply`/`destroy` cycle (W4) for recording the demo video.**

Concretely:

| Artifact | Status | What it shows |
|---|---|---|
| **W1+W2+W3 Terraform module set** (VPC / IAM / RDS / ECS / ALB / SQS / ECR / CW Log) | Written, validated by single `terraform apply` in W4 demo prep, then `destroy` | Design judgement: how to wire a real cloud deploy |
| **ADR-001 through ADR-027** | Permanent | Decision provenance |
| **`docker compose`** running on Lightsail Tokyo VPS | Always-on, $5/mo | The actual demo URL recruiters click |
| **`bin/setup-vps.sh`** | New artifact, ships in W3 | VPS provisioning: docker install, compose pull, env config, systemd unit |
| **GitHub Actions CI/CD** | Dual target: `ecs-deploy` (manual trigger, run once W4) + `vps-deploy` (auto on main, SSH-based) | Both deployment paths real and tested |

### Why Lightsail Tokyo specifically

| Dimension | Choice | Why |
|---|---|---|
| Region | Tokyo (ap-northeast-1) | Taiwan ↔ Tokyo latency 30-50ms; deciders interview Taiwan companies primarily |
| Provider | AWS Lightsail | Keeps the "AWS" resume keyword; same console / billing as the validated Fargate Terraform; recruiters in Taiwan recognise the AWS brand more than Vultr/Linode |
| Plan | $5/mo (2 GB RAM, 1 vCPU) | 2 GB headroom needed: Postgres ~150 MB + 5 Python services × ~100 MB + OS ≈ 700-900 MB used; 1 GB would force swap |
| OS | Ubuntu 24.04 LTS (selected when Lightsail blueprint chosen) | Standard, long support window, deciders' Compose-native environment |

### Alternatives considered (and rejected)

- **Vultr Tokyo $6/mo (1 GB RAM)** — pure-VPS narrative cleaner, but 1 GB tight
  for the 5-service stack; rejected for stability of demo.
- **Hetzner CX11 5 EUR (Germany)** — initially recommended in the grilling
  session; rejected on 250-280ms Taiwan latency.
- **Oracle Free Tier (Tokyo, ARM A1, 24 GB RAM, $0)** — extraordinary specs but
  free-tier instances are routinely killed by Oracle without warning; "demo
  disappeared the day before the interview" risk too high for portfolio context.
- **Contabo Singapore $4.50/mo (8 GB)** — CP value highest, but Singapore
  latency 60-90ms (worse than Tokyo) and Contabo brand carries no recruiter
  weight; oversubscribed IO can cause demo lag.
- **GCP e2-micro free tier (Tokyo)** — always free, but shared vCPU under 5
  Python services + Postgres becomes erratic; demo reliability concern.

## Consequences

### Resume narrative (positive)

The pivot strengthens, not weakens, the portfolio story:

> "I designed an AWS lift-and-shift architecture for the task scheduler — ECS
> Fargate, RDS, ALB, Multi-AZ, with Terraform IaC and GitHub Actions CI/CD. The
> idle cost would run ~$120/mo. While job-searching I run the workload on AWS
> Lightsail Tokyo for $5/mo (a managed VPS in the same provider), reserving the
> Fargate path for when scale or reliability requirements justify it. One
> `terraform apply` reconstitutes the full Fargate topology in ~10 minutes;
> I've validated this end-to-end."

This is a **cost-aware engineering judgement** signal — stronger than running
unused capacity 24/7 to display the AWS keyword.

### Cost (positive)

- Monthly recurring: **$5** (Lightsail) vs **$117-145** (Fargate path)
- One-time AWS validation cost: ~$5-10 for a few hours of running during W4
  demo recording
- 12-month projected total (active job search): **$70-80** vs **$1400-1700**

### Operational (mixed)

- VPS hardening becomes the deciders' responsibility: SSH key auth, ufw,
  fail2ban, automated security updates — not negligible but well-documented.
- No Multi-AZ HA on the VPS: a Lightsail Tokyo outage takes the demo down.
  Acceptable for portfolio context; documented as a known limitation in
  `README.md`.
- Daily ops digest (Action Sprint) runs on the VPS — real production-like
  workload, runs without manual intervention.

### Implications for the W3 grilling (ADR-024 amendments)

ADR-024 Tier 1/2 items that depended on the AWS-as-always-on model now branch:

| Item | Status |
|---|---|
| T1.1 Terraform module set | Still ship — code is the artifact |
| T1.2 Network topology (ADR-025 P1) | Still ship in Terraform — validated by one-shot apply |
| T1.3 RDS connection pool reconciliation | Document for AWS target; on VPS single-process Postgres there is no 81-connection ceiling concern |
| T1.4 GitHub Actions CI/CD | **Dual-target**: `ecs-deploy` (manual workflow_dispatch) + `vps-deploy` (auto on main, SSH-based) |
| T1.5 AWS Budgets alert | Still ship — covers the one-shot validation cost ceiling |
| T1.6 HTTPS via ACM | For Fargate target; on VPS use Caddy with auto-ACME instead |
| T1.7 Alembic migration as ECS task | Still ship for Fargate target; on VPS migration runs via `docker compose run migrate` |
| T2.1 Declarative partition | Still ship in migration |
| T2.2 RDS Multi-AZ posture | Decided as "off" (single-AZ is W3 default; Multi-AZ stays as Terraform variable) |
| T2.3 Secrets via Secrets Manager | Fargate target; on VPS use `.env` file with 0600 perms + Hetzner Hetzner-style backups |
| T2.4 Post-deploy smoke test | Still ship — runs against VPS URL on every push |

The Q-W3-4b cost-posture analysis (M1-M4 always-on vs paused, JS-1 to JS-4
job-search postures, wake-on-demand Lambda design) becomes **portfolio
narrative material** rather than implementation. The reasoning trail —
"evaluated $0-145/mo postures, settled on $5/mo VPS for the running workload" —
is itself a talking point in interviews.

## References

- Grilling Session #4 transcript (Q-W3-4b cost-posture discussion)
- ADR-024 (W3 tier scoping — original Fargate cost recalculation)
- AWS Lightsail pricing (Tokyo region, $5 plan: 2 GB RAM / 1 vCPU / 60 GB / 3 TB)
- AWS Public IPv4 surcharge announcement (2024-02), included in Fargate cost
- Hetzner / Vultr / Linode / Oracle / Contabo / GCP latency benchmarks from
  Taiwan (informal, deciders' research 2026-05-17)
