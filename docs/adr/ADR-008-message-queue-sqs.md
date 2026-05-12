# ADR-008: Message queue — AWS SQS in prod, ElasticMQ locally

- **Status**: Accepted
- **Date**: 2026-05-12
- **Source**: .doc/session/grilling-state.md Q8
- **Related**: ADR-003 (Postgres), ADR-007 (Watcher HA), ADR-004 (ECS Fargate)

## Context

We need a durable buffer between Watcher and Worker that supports:

- At-least-once delivery semantics (deduped at Worker via `claim-and-mark`).
- Visibility timeout + heartbeat extension while long actions run.
- DLQ routing after N failed deliveries.
- Delayed delivery (≤ 5 min) so messages aren't visible before `scheduled_at`.

## Decision

**AWS SQS standard queues** in production; **ElasticMQ** locally (single Docker container with SQS-compatible API). One Watcher publishes; multiple Workers consume.

Settings:

- Initial **visibility timeout: 60 s**; extended via `ChangeMessageVisibility` every 30 s by the Worker heartbeat.
- **`MaxReceiveCount = 3`**; on the 4th delivery attempt, SQS routes to the DLQ automatically. App code does nothing.
- **`DelaySeconds`** ≤ 5 min so workers don't see a message before `scheduled_at`. SQS's max is 15 min.

## Alternatives considered

- **SNS + SQS fanout** — overkill for our point-to-point Watcher→Worker channel.
- **Kafka / Kinesis** — overkill at this scale; ops burden; cost.
- **Postgres LISTEN/NOTIFY** — no durable buffer, no DLQ, no built-in retry; would re-implement SQS in app code.
- **Redis Streams** — durable enough but ops burden grows fast (memory pressure, persistence config). Less AWS-narrative.
- **RabbitMQ / Amazon MQ** — managed but more expensive; AWS RabbitMQ broker has a 1-hour minimum charge model.

## Consequences

- AWS narrative density +1; SQS appears in every cloud-architecture diagram for this role.
- Free tier covers the prototype.
- D-1 path (W4 Lambda-based worker) is native — SQS is a first-class Lambda event source.
- ElasticMQ behaviour may diverge from real SQS (FIFO semantics, MessageAttributes edge cases). Mitigation: integration tests against real SQS in W3 staging.
- The 15-min `DelaySeconds` ceiling rules out using SQS as a long-range scheduler — confirming the Watcher's lookahead-window design.
