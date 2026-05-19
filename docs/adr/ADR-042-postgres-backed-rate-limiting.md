# ADR-042: Postgres-backed rate limiting for task.create.v1

**Status:** Accepted  
**Date:** 2026-05-19  
**Deciders:** PaynePew  
**Issue:** #96

---

## Context

The public demo URL uses trust-only auth (ADR-024): any caller that reaches the MCP
endpoint can create jobs as `MCP_USER_ID`.  Without a rate limit, a runaway agent loop
or curious robot can exhaust the Postgres connection pool, flood the queue, and incur
non-trivial RDS cost in under a minute.

Two requirements drove the design:

1. **Two windows** — a per-day cap (default 1000) to prevent runaway overnight loops,
   and a per-minute burst cap (default 10) to stop rapid-fire creation.
2. **No new infrastructure** — the project already owns one Postgres instance (ADR-011);
   adding Redis for rate limiting would double the infrastructure surface for a use case
   that currently handles a single user.

---

## Decision 1 — Postgres-over-Redis

The rate limiter issues two `SELECT COUNT(*), MIN(created_at)` queries against the
existing `jobs` table, filtered by `(user_id, created_at > now() - interval)`.

**Why:**

- Zero new dependency: the `jobs` table is already authoritative for per-user job
  history; counting rows in a time window is exactly what a B-tree index on
  `(user_id, created_at)` is built for.
- Multi-replica consistency: every `mcp-server` replica shares one Postgres, so the
  burst count is globally accurate (modulo the TOCTOU window noted below).
- Ops simplicity: one backup target, one set of credentials, one connection pool.

**Why not Redis:**

- Adds a second stateful service to provision, back up, and monitor.
- Atomic `INCR` + `EXPIRE` in Redis would be marginally faster and free of TOCTOU, but
  at sub-10 req/s (the current steady state) the difference is imperceptible.
- Redis becomes the right choice at sustained > 10 req/s — see trigger conditions.

---

## Decision 2 — Reuse existing index

`idx_jobs_user_created` (`user_id`, `created_at DESC`), created in migration `0001`,
covers both rate-limit queries:

```sql
SELECT COUNT(*), MIN(created_at) FROM jobs
WHERE user_id = $1 AND created_at > $2
```

Postgres resolves this as an index-scan on the leading `user_id` column followed by a
range scan on `created_at`.  No new migration is required while `jobs` < 1 M rows.

---

## Decision 3 — Separate session for the rate-limit check

`check_rate_limit` runs `session.execute()` in its own session context manager,
separate from the session passed to `create_job`.  This avoids a
`InvalidRequestError: A transaction is already begun` conflict: `check_rate_limit`
starts an implicit read transaction, and `create_job` immediately calls `session.begin()`
on the same object.

The two-session approach introduces a small TOCTOU window (a concurrent request may slip
in between the count query and the insert), but this is acceptable for a best-effort
rate limiter: the intent is to block loops and robots, not to provide hard guarantees.

---

## Decision 4 — env-configurable limits

| Env var                    | Default | Description              |
|----------------------------|---------|--------------------------|
| `RATE_LIMIT_DAILY`         | 1000    | Max jobs per user per 24h |
| `RATE_LIMIT_BURST_PER_MINUTE` | 10  | Max jobs per user per minute |

---

## Limitations

The following limitations are **explicit and accepted** for the W4 scope:

1. **In-process burst window is not shared across replicas.**  Each `mcp-server`
   replica issues its own COUNT query, so the effective burst limit under `N` replicas
   is `burst × N`.  Example: 3 replicas with burst=10 allows up to 30 jobs/min across
   the fleet before any single replica rejects.  The daily window is unaffected because
   all replicas count from the same persistent `jobs` table.

   *Mitigation*: the demo VPS runs a single replica.  Redis upgrade resolves this.

2. **TOCTOU window between check and insert.**  A small race exists between the COUNT
   query and the INSERT in `create_job`.  Under concurrent load, a user at the limit
   could slip an extra job through.  Acceptable for anti-robot / loop protection.

3. **`retry_after_seconds` is approximate.**  The value is computed from the oldest
   job in the window: `ceil((oldest_created_at + window_duration - now).total_seconds())`.
   Clock skew between the app server and Postgres (typically < 1 ms) can cause ±1 s
   variance.

4. **Counts all jobs, including cancelled/failed.**  A user who creates and immediately
   cancels 1000 jobs per day is still rate-limited.  This is intentional — cancelled
   jobs still consumed scheduler resources on creation.

---

## Trigger conditions for Redis upgrade

Switch to a Redis sliding-window counter when **any** of the following is true:

- Sustained request rate > 10 req/s per user.
- `jobs` table > 1 M rows (COUNT scans become slow even with the index).
- Replica count > 4 (burst leakage becomes significant).

---

## Why not OAuth/auth-based limits

ADR-024 established trust-only auth for W1–W4.  Adding per-user OAuth scopes would
require client registration, token issuance, and token refresh logic — a full auth
surgery.  Rate limiting is the minimum viable abuse mitigation that fits within the
existing trust-only model without blocking the W4 sprint.

---

## Consequences

- `task.create.v1` returns `{"ok": false, "error": {"code": "USER_INPUT", "message":
  "Rate limit exceeded: <daily|burst>", "field": "user_id", "expected":
  {"retry_after_seconds": N}}}` when either limit is exceeded.
- Two extra SELECT queries per `task.create.v1` call.  Under the existing load profile
  (< 100 creates/day on the demo), this is negligible.
- `RATE_LIMIT_DAILY` and `RATE_LIMIT_BURST_PER_MINUTE` must be added to `.env.example`
  and VPS deploy config so operators can tune limits without a code change.
