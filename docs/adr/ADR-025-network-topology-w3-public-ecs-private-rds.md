# ADR-025: W3 network topology — public ECS tasks, private RDS, no NAT Gateway

- **Status**: Accepted
- **Date**: 2026-05-17
- **Deciders**: PaynePew
- **Source**: Grilling Session #4, Q-W3-2
- **Related**: ADR-004 (ECS Fargate), ADR-005 (ALB front door), ADR-018 (no server-side LLM in W2), ADR-024 (W3 tier scoping)
- **Learn doc**: `.doc/learn/aws-vpc-networking.md`

## Context

W2 PRD enumerated three candidate VPC topologies for W3:

- **P1**: ECS tasks in public subnet (with assigned public IP)
- **P2**: ECS tasks in private subnet + NAT Gateway
- **P3**: ECS tasks in private subnet + VPC Endpoints only (no internet egress)

Two facts narrow the field before evaluating P1 vs P2:

1. **P3 is structurally incompatible with the `http_call` action.** `http_call`
   targets user-supplied URLs (arbitrary internet endpoints). VPC Endpoints only
   reach AWS services. Without internet egress the action cannot work, so P3
   would force either removing `http_call` (kills a W1 deliverable) or whitelisting
   endpoints (kills the demo's flexibility).

2. **ADR-018's β-cut removed the original P3 motivation.** P3 was attractive when
   the worker needed Bedrock egress (for `llm_summarize`); since that action was
   cut, the only remaining outbound traffic is `http_call` (arbitrary URLs) and
   AWS service APIs.

Therefore the decision reduces to **P1 vs P2**.

## Decision

**P1 (public ECS + private RDS), 2 AZ, S3 Gateway Endpoint enabled, no NAT Gateway.**

### Subnet layout

```
VPC 10.0.0.0/16
├── public  10.0.0.0/24   (us-east-1a)   ALB + ECS tasks
├── public  10.0.1.0/24   (us-east-1b)   ALB + ECS tasks
├── private 10.0.10.0/24  (us-east-1a)   RDS primary
└── private 10.0.11.0/24  (us-east-1b)   RDS standby slot (Multi-AZ optional)
```

### Security group layering

| SG | Inbound rule | Source |
|---|---|---|
| `alb-sg` | 443 | `0.0.0.0/0` |
| `ecs-tasks-sg` | 8080 | `alb-sg` (SG reference, not IP) |
| `rds-sg` | 5432 | `ecs-tasks-sg` (SG reference) |

ECS tasks have `assignPublicIp = ENABLED` (required for Fargate to pull ECR
images without NAT). Inbound is closed because `ecs-tasks-sg` only allows
ingress from `alb-sg`.

### VPC Endpoints

- **S3 Gateway Endpoint**: enabled (free). ECR image-layer pulls route through
  it, saving public IGW traffic for the large blob portion of image pulls.
- **Interface Endpoints**: none. SQS / Secrets Manager / CloudWatch Logs / ECR
  API are reached via public IGW. Traffic volume is well under $7/AZ/month
  threshold where Interface Endpoints become cost-positive.

## Alternatives considered

### P2 (private ECS + NAT Gateway)

- **Cost**: $32.40/mo per AZ × 2 AZ = **$64.80/mo** fixed + $0.045/GB processed.
  Single-AZ NAT ($32.40) would create a cross-AZ data-transfer hotspot and a
  single point of failure that defeats the Multi-AZ posture elsewhere.
- **Security gain over P1**: marginal in this design. P2 tasks are unreachable
  from the internet because their ENI has no public IP; P1 tasks have a public
  IP but `ecs-tasks-sg` accepts inbound only from `alb-sg`. The effective inbound
  attack surface is identical (the ALB).
- **Talking-point loss**: "I evaluated NAT vs public subnet and chose the cheaper
  option with documented threat model" reads stronger than "I followed the
  default playbook".

### P1 with 1 AZ instead of 2

Rejected: ALB requires `≥ 2` subnets across `≥ 2` AZ to provision; RDS subnet
group also requires multiple AZ; 2 AZ is the floor not the ceiling.

### Interface Endpoints for SQS / ECR / Secrets Manager

Rejected for W3 on cost grounds: $7.2/AZ/month each, × 2 AZ × 3-4 endpoints =
$43-58/mo overhead — exceeds the entire NAT cost we just avoided. Portfolio
traffic volumes do not amortise the fixed cost.

S3 Gateway is the exception because it is **free**, not because S3 traffic is
unusually large.

## Consequences

- **No NAT Gateway in W3 Terraform.** A future migration to P2 is a single
  variable flip (`enable_nat_gateway = true`) + a small module include — the
  rest of the topology (route tables, subnet IDs, SGs) is shaped to accept a
  NAT swap in.
- **Public IPv4 charge applies.** Since 2024-02 every public IPv4 costs
  $0.005/hr ≈ $3.6/mo per task. With 7 idle tasks (ADR-026 service count) this
  is ~$25/mo. Still cheaper than NAT.
- **ECR image-layer traffic is free** via the S3 Gateway Endpoint; ECR API
  metadata calls travel public IGW (KB-scale, free or sub-cent).
- **`http_call` action egresses via public IGW directly.** First 100 GB/mo of
  internet egress per region is free tier; portfolio usage will not exceed it.
- **RDS is in private subnets.** Cross-environment connection from a developer
  laptop requires either a bastion host (not built in W3) or a temporary public
  endpoint flag (W3 dev pattern: `publicly_accessible = true` gated by SG to
  developer IP). Operational note for the post-deploy iteration loop.

## Talking points for the portfolio narrative

> "I evaluated NAT Gateway versus a public-subnet placement for the ECS tasks.
> NAT would add ~$64/month fixed cost across two AZs plus $0.045/GB processed,
> in exchange for moving the tasks behind a layer of subnet isolation. The
> effective attack surface is the ALB in both designs because the task security
> group only accepts inbound from the ALB SG. I chose the public-subnet
> placement, documented the trade-off, and the migration path to NAT is a single
> Terraform variable."

## References

- Learn doc: `.doc/learn/aws-vpc-networking.md` (sections 4 NAT internals,
  5 VPC Endpoint, 7 P1 packet walk-through)
- W1 PRD `docs/PRD/prototype-w1.md` (130 vs 81 connection conflict — separate ADR)
- W2 PRD `docs/PRD/bonus-w2.md` "Out of scope for W2, in scope for W3"
- AWS Public IPv4 charge announcement (2024-02-01)
- ADR-018 (no server-side LLM in W2) — the β-cut that vacated P3 motivation
