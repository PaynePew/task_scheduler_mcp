# ADR-007: Watcher high availability — multi-process + FOR UPDATE SKIP LOCKED, no leader election

- **Status**: Accepted
- **Date**: 2026-05-12
- **Source**: doc/session/grilling-state.md Q7
- **Related**: ADR-003 (Postgres), ADR-009 (schema)

## Context

The Watcher process scans `job_runs` for work due within the lookahead window (5 min) and publishes to the queue. If we run a single Watcher, it's a single point of failure. If we run multiple, they will compete for the same rows unless coordinated.

## Decision

Run **multiple Watcher processes concurrently** (planned 2–3 in W3). Coordination is delegated to Postgres via `SELECT ... FOR UPDATE SKIP LOCKED` in the watcher's claim query. There is **no leader election**, no ZooKeeper, no Redis lock.

The Watcher transaction:

1. `SELECT ... FROM job_runs WHERE status='PENDING' AND scheduled_at < now()+INTERVAL '5 minutes' FOR UPDATE SKIP LOCKED LIMIT N`
2. `UPDATE job_runs SET status='QUEUED' WHERE run_id IN (...) RETURNING ...`
3. `INSERT INTO run_events ...` (one event per claimed run)
4. `aws sqs send-message-batch ...` outside the transaction (idempotent: at-most-once is acceptable; the row is already QUEUED).
5. `COMMIT`

## Alternatives considered

- **Single Watcher + hot standby** — single point of failure during failover; needs health checks and a manual or automated cutover.
- **Leader election (ZooKeeper / etcd / Redis Redlock)** — operational complexity for a feature Postgres gives us for free. Adds another service to deploy.
- **App-level distributed locks** — fragile; the same problem in disguise.

## Consequences

- Horizontal scaling is trivial — add another Watcher ECS task and it joins the pool.
- The pattern is well-trodden in production: Oban (Elixir), River (Go), PgBoss (Node.js) all use SKIP LOCKED.
- We must explain "why no leader election" in interviews — the answer is "Postgres already gives us mutual exclusion per-row; coordination is free".
- If SQS send fails after the row is updated to QUEUED, the row sits stuck. Mitigation: a recovery query for "QUEUED for too long" rows that re-enqueues them. This is a known operational concern; deferred to W2/W4.
