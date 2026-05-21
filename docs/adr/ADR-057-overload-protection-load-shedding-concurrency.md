# ADR-057: Overload protection posture — load shedding + concurrency limiting

- **Status**: Accepted
- **Date**: 2026-05-21
- **Deciders**: PaynePew
- **Source**: Grilling Session #6 (grill-with-docs) + *Overload Protection* primer
- **Related**: ADR-008 (SQS — queue load leveling already built), ADR-042/055 (rate limiting / quotas), ADR-026 (worker autoscale, Fargate), ADR-041 (Caddy edge), CONTEXT §8 (per-role connection pools)

## Context

The *Overload Protection* primer lays out a layered model: rate limiting →
concurrency limiting → queue-based load leveling → auto-scaling → load shedding +
request prioritization → backpressure. Mapping it onto this system:

- **Already built**: queue-based load leveling (`watcher → SQS → worker` is the
  shock absorber); rate limiting (ADR-042/055); per-role connection pools
  (a connection-pool bulkhead, CONTEXT §8); worker autoscale (Fargate only,
  ADR-026).
- **Missing**: concurrency limiting, load shedding, backpressure signalling.

ADR-055 is *policy-based* admission (per-user quotas, global ceiling). This ADR is
*health-based* protection: defend the box when it is actually hot, regardless of
who is sending.

## Decision

1. **Concurrency limiting** — bound in-flight work, not just arrival rate:
   - Worker concurrency tuned to the box (on 1 vCPU, *fewer* workers can be
     better — avoid context-switch / connection thrash); do not blindly add workers.
   - mcp-server request concurrency cap (semaphore) → return `503` when exceeded.
   - Per-role connection pools remain the DB-layer bulkhead.
2. **Load shedding** — health-based: a check (CPU / RAM / queue depth) trips a
   global admission gate that returns `503 + Retry-After`, **shed at the edge
   (Caddy) first**, before business logic.
3. **Backpressure** — when SQS depth exceeds a threshold, `task.create` returns
   `429 + Retry-After` (app-level slow-down signal).
4. **Rate-limit response** — on the HTTP transport, return proper
   `429 + Retry-After` (replacing the bare envelope) per the primer.

## Not doing now (backlog `docs/` D-items)

- Request prioritization (no user tiers yet; revisit with paid/free).
- Adaptive (latency-driven) concurrency limiting.
- Bulkhead beyond process-role + connection-pool separation (no read replica).

## Consequences

- A health-check utility + a global admission flag (shared with ADR-055's ceiling).
- Caddy edge `503` rule; `429 + Retry-After` responses from mcp-server.
- The "shed, don't crash" property: under 10× load the system serves a fraction
  with clear errors instead of total collapse — a portfolio/interview talking point.

## Module-state exemption

`app/overload/health.py` contains one piece of intentional module-level mutable
state: `_queue_depth_cache: dict[str, tuple[int, float]]`.  This is an explicit
exemption from the project's *no new module-level mutable state in long-running
processes* rule.  Rationale:

- **TTL-keyed and idempotent**: every entry is bounded by `_QUEUE_DEPTH_TTL`
  (30 s).  A stale entry produces a conservative (safe) depth reading, not a
  correctness error.
- **Single-key in practice**: the cache maps `queue_url → (depth, ts)`.  The
  deployed process has exactly one queue URL, so the dict stays a single entry.
- **Moving state to an instance is more churn than value**: `get_queue_depth` is a
  module-level helper; threading the cache through every call site would bloat the
  call surface without improving safety.

## Alternatives considered

- **Rate limiting alone** — rejected: bounds per-client arrival, not aggregate or
  health-driven overload.
- **Do nothing (rely on SQS)** — rejected: SQS absorbs *execution* spikes but the
  ingestion path (mcp-server, DB writes) and the single core still need a health
  backstop.
