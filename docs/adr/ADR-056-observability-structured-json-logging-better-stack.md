# ADR-056: Observability — structured JSON logging shipped to Better Stack

- **Status**: Accepted
- **Date**: 2026-05-21
- **Deciders**: PaynePew
- **Source**: Grilling Session #6 (grill-with-docs)
- **Related**: ADR-024 (deferred structured logging to W4 — promoted here), ADR-031 (Better Stack monitoring), ADR-049 (public pivot), ADR-050/032 (secrets — redaction), CONTEXT §8

## Context

Current logging (verified in code): every entrypoint calls stdlib
`logging.basicConfig` with a **plain-text** format to stdout
(`app/entrypoints/*.py`). There is no central config, no structured fields, no
shipping, no retention, no cross-service correlation. The only query path is
`docker logs | grep` on the box. Better Stack (ADR-031) is wired for **uptime**,
not log aggregation. ADR-024 deferred "structured JSON logging" to W4.

Under the public multi-tenant pivot this is a blocker, not a nicety: the incident
runbook's first step is "see what's happening", and right now that means grepping
a dying host with no history and no per-user correlation.

## Decision

**Structured JSON logging, shipped to Better Stack (Logtail), promoted from
W4-deferred to a pre-public prerequisite.**

- A **central logging config module** replaces the per-entrypoint `basicConfig`.
- JSON formatter (stdlib `logging` + `python-json-logger`, or `structlog`).
- Consistent fields on every line: `ts`, `level`, `service`/role, `event`,
  `git_sha` (already in `/healthz`), and correlation ids where present
  (`user_id`, `job_id`, `run_id`).
- Ship to **Better Stack** (already the monitoring vendor) → queryable, retained,
  alertable on log patterns.
- **Redaction discipline**: never log tokens/secrets. Log connection ids, not
  Layer-2 OAuth tokens (ADR-050); `${VAR}` templates are already stored unresolved
  (ADR-032) — keep that property in logs.

## Consequences

- One logging-config module; `log_level` (settings) drives it.
- New dependency (JSON formatter) + Better Stack log source/token in `.env`.
- Per-user correlation makes "what did user X's jobs do last night" answerable.
- Free-tier retention limits on Better Stack — acceptable for portfolio scale.

## Alternatives considered

- **Keep plain-text + `docker logs`** — rejected: ephemeral, unstructured, no
  cross-service query, no retention.
- **Self-host Grafana Loki** — rejected: extra RAM/ops on the $5 box.
- **CloudWatch Logs** — applies to the Fargate design target; for the VPS runtime,
  Better Stack (already in use) is the lower-friction choice.
