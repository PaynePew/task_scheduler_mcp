# ADR-024: W3 tier scoping — what ships in the AWS lift-and-shift, what defers

- **Status**: Accepted
- **Date**: 2026-05-17
- **Deciders**: PaynePew
- **Source**: Grilling Session #4, Q-W3-1
- **Related**: ADR-001 (project scope), ADR-004 (ECS Fargate), ADR-005 (ALB), ADR-008 (SQS), ADR-015 (user-identity-resolver-fallback), ADR-023 (W2 cut scope)

## Context

W2 PRD (`docs/PRD/bonus-w2.md` "Out of scope for W2, in scope for W3") listed seven
items as the W3 deliverable surface:

1. Terraform modules for ECS Fargate / RDS / ElastiCache / ALB / SQS / IAM / VPC
2. Network topology choice (P1 public-subnet / P2 NAT / P3 Bedrock VPC Endpoint)
3. GitHub Actions CI/CD (lint, test, build, ECR push, ECS deploy)
4. AWS Budgets alert + IAM least-privilege roles
5. ALB OIDC integration replacing step 2 of the user_id / timezone resolvers
6. RDS Proxy or pool downsizing decision (W1 PRD 130 vs 81 conflict)
7. `job_runs` declarative `PARTITION BY RANGE` + `pg_partman` automation cron

Between W2 PRD freeze (2026-05-16) and W3 grilling (2026-05-17) two structural
changes invalidated parts of that list:

- **β-cut server-side LLM** (ADR-018): removed the original motivation for P3
  (Bedrock VPC Endpoint), and removed the need for NAT egress to OpenAI/Anthropic.
- **SQS replaced Redis as the queue layer** (ADR-008): ElastiCache no longer has a
  consumer in the W2 design. The "ElastiCache module" line item in (1) is now an
  artefact of the original W1 plan.

W3 must therefore re-scope before opening the Terraform editor.

## Decision

Three-tier scoping mirroring ADR-023's pattern:

### Tier 1 — Must ship

| # | Item | Why must-ship |
|---|---|---|
| T1.1 | Terraform module set: VPC / IAM / RDS / ECS Fargate / ALB / SQS / ECR / CloudWatch log group | Portfolio + course assignment hard requirement |
| T1.2 | Network topology choice | Blocks every other infra decision (ADR-025 resolves) |
| T1.3 | Connection-pool ↔ RDS `max_connections` reconciliation | W1 PRD raised a 130 vs 81 conflict that ships unresolved otherwise |
| T1.4 | GitHub Actions CI/CD: lint → test → docker build → push ECR → ECS deploy | "CI/CD" is a resume keyword that the portfolio cannot lack |
| T1.5 | AWS Budgets alert ($10 / $50) + IAM least-privilege task roles + ECR repository policy | Cost-safety baseline; trivial cost but expensive to forget |
| T1.6 | HTTPS via ACM + a demo-able URL (ALB DNS or custom domain) | The acceptance gate's L3 step requires a reachable URL |
| T1.7 | Alembic migration as a one-shot ECS task triggered pre-deploy | Same migration story as local Compose, no schema drift across env |

### Tier 2 — Should ship

| # | Item | Why should-ship |
|---|---|---|
| T2.1 | `job_runs` declarative `PARTITION BY RANGE` (structural change only) | Closes the W1 / W2 deferral chain; partition shape is now stable |
| T2.2 | RDS Multi-AZ posture decision (on / off) — a required Terraform var | Cannot defer; not deciding is deciding `off` |
| T2.3 | Secrets via AWS Secrets Manager (DB password at minimum) | Avoids the "DB password in Terraform variable" anti-pattern in portfolio code |
| T2.4 | Smoke test step in CI/CD: post-deploy `task.create` against live URL | Catches deploy-time regressions cheaply |

### Tier 3 — Deferred, with rationale

- **ElastiCache module — cut entirely.** ADR-008 replaced Redis with SQS as the
  queue layer; no other component in the design uses Redis. Saves ~$12/mo and a
  Terraform module. Re-introduction path: if a future feature needs caching, the
  same Terraform pattern (cache subnet group + cache cluster + SG) can land in a
  single PR — there is no architectural debt being created by this cut.

- **ALB OIDC + Cognito integration — defer to W4 polish (D-X).** The user_id
  resolver chain (ADR-015) already supports a header path (`X-User-Id`) for the
  W3 demo. Adding OIDC requires learning Cognito User Pool + Hosted UI + token
  claim mapping + ALB listener rule rewrites — half-to-full day with low ROI for a
  backend/infra resume narrative (OIDC reads more as a frontend/auth signal).
  The resolver code reserves a documented swap point so W4 can land OIDC as a
  one-PR change.

- **`pg_partman` automation cron — defer to W4.** T2.1 ships the partition
  *structure* (`PARTITION BY RANGE` + the first partition); the *automation*
  (cron job that creates next-period partitions and drops aged ones) has no
  verification surface during a 1-week W3 — there is no live data to age out.
  Building it without a workload to validate it against creates code that will
  silently rot. Defer to W4 alongside an observability story.

- **Multi-region / read replica / WAF** — remains in the system-design.md (D-2),
  (D-3), (D-23) backlog. No portfolio-narrative benefit at this stage.

- **CloudWatch dashboard / alarms / structured JSON logging** — already W4 scope
  per W2 PRD; restated here for completeness.

## Consequences

- The Terraform module surface for W3 is **VPC / IAM / RDS / ECS / ALB / SQS / ECR
  / CloudWatch log group / Secrets Manager** — no ElastiCache, no Cognito, no
  WAF, no pg_partman.
- The 130 vs 81 RDS connection conflict from W1 PRD must be resolved in this wave
  (separate ADR, follow-up grilling question).
- The auth posture is "trust-the-header" in W3 cloud (same as W1 local) — the
  ADR-015 fallback chain is the documented surface.
- `pg_partman` deferral is reversible: the partition structure landing in T2.1
  is `pg_partman`-compatible (range partitioning on a date-like column), so W4
  automation is a config layer, not a re-architecture.

## Alternatives considered

- **Ship everything in the original W3 list** — would push timeline ~3 days and
  add ElastiCache complexity for zero benefit (no Redis consumer in current design).
- **Ship only Tier 1, cut Tier 2** — partition deferral creates a third "I'll do
  it later" promise that has been deferred since W1; the structural change is
  small (one migration) and best paid down now while schema is fresh.
- **Cut HTTPS / ACM (T1.6)** — would force the acceptance gate to be tested over
  plain HTTP, which is implausible for MCP clients in 2026 and tanks the demo
  story. Kept.

## Open questions resolved in follow-up ADRs

| Item | Resolution |
|---|---|
| T1.2 network topology | ADR-025 |
| T1.3 connection pool reconciliation | TBD (next grilling question) |
| T2.2 RDS Multi-AZ posture | TBD |
| T2.1 partition migration shape | TBD |
