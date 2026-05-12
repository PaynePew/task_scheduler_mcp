# ADR-003: Primary data store — Postgres 16

- **Status**: Accepted
- **Date**: 2026-05-12
- **Source**: internal grilling session Q4 (local-only, not in git)
- **Related**: ADR-008 (queue), ADR-009 (schema)

## Context

Non-functional requirement states "10K jobs/sec peak". The course material suggests DynamoDB or Cassandra "due to 10K writes/sec". Our access patterns also need:

- Atomic claim-and-mark semantics across multiple worker processes.
- Outbox pattern (status update + event insert in one transaction).
- Per-user listing newest-first with offset pagination.
- Range queries over `scheduled_at` within a `time_bucket`.

## Decision

**Postgres 16** as the primary data store. The 10K spec is interpreted as peak **front-door scheduling requests**, not sustained worker throughput; SQS absorbs the burst (see ADR-008). Postgres + `FOR UPDATE SKIP LOCKED` + native `PARTITION BY RANGE` + transactional outbox outperform DynamoDB for our access patterns at this scale.

## Alternatives considered

- **DynamoDB** — strong write throughput but no native transactional outbox semantics; range queries over time within a partition require careful key design; multi-row atomic claim is awkward. Streams add infra coupling.
- **Cassandra** — same write-throughput story; even weaker for our SQL-shaped access patterns.
- **MySQL** — `FOR UPDATE SKIP LOCKED` available since 8.0, but native partitioning is weaker, and JSONB story trails Postgres.

## Consequences

- We commit to a single-writer Postgres in W3; horizontal scale path is RDS Proxy + read replicas (for read-heavy bonuses) and sharding only if 10K is confirmed sustained (open question with instructor).
- `FOR UPDATE SKIP LOCKED` becomes the load-bearing primitive — the same pattern used in Oban / River / PgBoss.
- We must defend "Postgres over DynamoDB" in interview; the answer-back is documented in `.doc/learn/course-spec.md` § 10.
