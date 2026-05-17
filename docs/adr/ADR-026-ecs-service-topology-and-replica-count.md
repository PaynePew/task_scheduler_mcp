# ADR-026: W3 ECS service topology — five long-running services, fixed replicas, worker autoscaling

- **Status**: Accepted
- **Date**: 2026-05-17
- **Deciders**: PaynePew
- **Source**: Grilling Session #4, Q-W3-3
- **Related**: ADR-004 (ECS Fargate), ADR-007 (watcher-ha-skip-locked), ADR-009 (database-schema-outbox), ADR-024 (W3 tier scoping), ADR-025 (network topology)

## Context

W1 design (Q10, CONTEXT.md §4) defines six process roles, each its own entrypoint
sharing a single Docker image:

| Role | Scaling characteristic from W1 |
|---|---|
| `mcp-server` | Stateless, scale horizontally |
| `watcher` | Multiple instances safe via `FOR UPDATE SKIP LOCKED` |
| `worker` | Multiple instances safe via claim-and-mark |
| `recurring_watcher` | Single instance for W1 |
| `chain_watcher` | Single instance for W1 |
| `migrate` | One-shot before app services start |

For W3 cloud deployment, three orthogonal decisions had to be made:

- **Service consolidation**: keep five long-running services or merge?
- **Replica count per service**: fixed or autoscaling, and at what counts?
- **Autoscaling strategy**: which dimensions, on which services?

## Decision

### Service consolidation: keep five long-running services (A1)

No merger of `recurring_watcher` and `chain_watcher` into a combined "reactor"
service, and no merger of all watchers into a single "scheduler". Each W1 role
remains its own `aws_ecs_service` resource. `migrate` runs as a one-shot task,
not a long-running service.

### Replica count

| Service | min | max | default | HA rationale |
|---|---|---|---|---|
| `mcp-server` | 2 | 2 | 2 | ALB requires ≥ 2 healthy targets for rolling deploy without downtime |
| `watcher` | 2 | 2 | 2 | Realises ADR-007's `FOR UPDATE SKIP LOCKED` HA design |
| `worker` | 1 | 4 | 1 (idle) → 4 (peak) | Only service with elastic load (SQS-driven) |
| `recurring_watcher` | 1 | 1 | 1 | W1 spec; event reactor, lag-tolerant |
| `chain_watcher` | 1 | 1 | 1 | W1 spec; event reactor, lag-tolerant |
| `migrate` | 0 | — | one-shot | Pre-deploy Alembic run |

Idle footprint: **7 tasks**. Peak: **10 tasks**. Each task one ENI + one public
IPv4 (per ADR-025).

### Autoscaling

Application Auto Scaling **enabled only on `worker`**, using SQS
`ApproximateNumberOfMessagesVisible` as the target tracking metric:

```
scale out when queue depth > 10 (towards max=4)
scale in  when queue depth < 5  (towards min=1)
```

`mcp-server` autoscaling on ALB request count is intentionally not configured —
portfolio traffic profile lacks the signal range for autoscaling to trigger.
`watcher`, `recurring_watcher`, `chain_watcher` are not autoscaled because their
load is bounded by DB cursor consumption, not by external arrival rate.

## Alternatives considered

### A2: merge `recurring_watcher` + `chain_watcher` → `reactor`

**Pros**: -1 task ≈ -$3.60/mo Public IPv4, -1 connection pool, -1 ECS service
definition.

**Cons (deciding against)**:
- Process boundary erasure makes one deploy restart both reactors (blast radius
  up).
- ADR-007 + ADR-009 model the two reactors as independent `processed_by` cursor
  consumers; merging adds an in-process coordination concern that did not exist.
- Cost savings (-$3.60/mo + a small Fargate sliver) are immaterial against the
  architectural-narrative cost.

### A3: merge all watchers (`watcher` + `recurring_watcher` + `chain_watcher`)

Rejected on the same grounds as A2, more strongly: `watcher` is N=2 for HA via
SKIP LOCKED; the single-instance reactors cannot run N=2 without a leader-
election design that W3 explicitly does not include.

### Fixed N=2 for `worker` instead of autoscaling 1→4

Rejected because:
- Autoscaling on SQS depth is a resume-keyword talking point with a real metric
  driving real ECS API calls — worth the small additional Terraform.
- N=2 idle would cost ~$3.60/mo more in Public IPv4 with no functional gain
  during portfolio's idle hours.

### `mcp-server` autoscaling on ALB request count

Rejected: portfolio request volume profiles flat-zero with sporadic test
traffic; the target-tracking metric would either always scale in (to min=2) or
never scale out (since traffic doesn't sustain). No portfolio-narrative gain.

## Consequences

- Five `aws_ecs_service` resources + one `aws_ecs_task_definition` per service
  (all referencing the same shared image, with different `command` overrides).
- One `aws_appautoscaling_target` + one `aws_appautoscaling_policy` on the
  `worker` service only.
- Idle cost (Public IPv4 alone): 7 × $3.6 ≈ $25.20/mo (ADR-025).
- The `recurring_watcher` / `chain_watcher` N=1 means a Fargate task restart
  causes ~30-60 seconds of reactor lag (no event loss — events are stored in
  `run_events`, the next instance resumes via the `processed_by` cursor).
- Future move to leader-elected N=2 reactors is a W4 (D-X) candidate; would need
  a leader-lock mechanism (Postgres advisory lock, DynamoDB lock table, or
  ECS-native eventually).

## References

- W1 PRD `docs/PRD/prototype-w1.md` (process role descriptions)
- ADR-007 (`FOR UPDATE SKIP LOCKED` watcher HA)
- ADR-009 (database schema + outbox `processed_by` cursors)
- ADR-024 (W3 tier scoping — this ADR is T1.1 territory)
- AWS Application Auto Scaling docs (target tracking on SQS metrics)
