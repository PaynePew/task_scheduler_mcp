# ADR-004: Cloud deployment path — AWS lift-and-shift on ECS Fargate

- **Status**: Accepted
- **Date**: 2026-05-12
- **Source**: internal grilling session Q5 (local-only, not in git)
- **Related**: ADR-005 (front door), ADR-010 (module layout)

## Context

The portfolio narrative requires a real cloud deployment. Options on AWS span Lambda-only (serverless), ECS Fargate (managed containers), EKS (Kubernetes), and self-managed EC2. Constraints: 1-week W3 budget, must be debuggable solo, cost predictable.

## Decision

**Path (A): AWS lift-and-shift to ECS Fargate** with RDS Postgres + ElastiCache (W2 bonuses) + ALB + SQS + Terraform IaC + GitHub Actions CI/CD. The 6 logical processes (mcp-server / watcher / worker / recurring-watcher / chain-watcher / migrate) each become an ECS Fargate service. Path (D-1) — a Lambda-based worker variant — is a W4 stretch goal for the trade-off blog post.

## Alternatives considered

- **Lambda-only** — cold-start cost hurts MCP request latency; connection-pool semantics with RDS are awkward (RDS Proxy mandatory); 15-min execution limit constrains long actions.
- **EKS / Kubernetes** — over-engineering for a 4-week project; massive learning surface; the resume-signal gain over ECS Fargate is small for this role.
- **EC2 self-managed** — weaker resume signal than managed; AMI/patching distractions.

## Consequences

- W3 ships Terraform modules per service; Dockerfile is shared across all 6 entrypoints.
- ALB chosen as the cloud front door (see ADR-005) — terminates TLS, routes to the mcp-server target group.
- Cost: ECS Fargate + RDS t4g.micro + small ALB ≈ within free-tier and modest budget; AWS Budgets alert mandatory.
- Lambda-comparison blog post becomes a portfolio asset (W4).
