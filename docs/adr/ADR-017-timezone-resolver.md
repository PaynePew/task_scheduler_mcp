# ADR-017: Timezone Resolver — 4-Step Fallback Chain

**Status:** Accepted  
**Date:** 2026-05-16  
**Issue:** #43 (S02: Recurring cron expansion + timezone resolver)

---

## Context

Recurring cron expressions are meaningless without a timezone: `0 8 * * *` at "8 AM" means something different in New York vs. Tokyo. The MCP client (an LLM agent) typically doesn't know the user's local timezone and the user rarely supplies it explicitly. We need a precedence chain that:

1. Honours an explicit caller-supplied value when present
2. Falls back gracefully to server-side signals
3. Never silently rejects a timezone string without explaining why
4. Is transparent enough that a future W3 upgrade (ALB OIDC) can slot in without breaking callers

The user identity resolver in ADR-015 (`MCP_USER_ID` → header → env → default) established the precedence-chain pattern; this ADR follows the same structure.

---

## Decision

### 4-step fallback chain

| Step | Source | Example value |
|------|--------|---------------|
| 1 | `timezone` argument in the tool call | `"Asia/Taipei"` |
| 2 | `X-Timezone` request header (future: ALB OIDC `sub` enrichment) | `"America/New_York"` |
| 3 | `MCP_USER_TZ` environment variable | `"Europe/Berlin"` |
| 4 | Hard-coded fallback | `"UTC"` |

The first step whose value is a valid **IANA key** (resolvable by `zoneinfo.ZoneInfo`) wins. Invalid values (offset strings like `UTC+8`, `+08:00`, Windows IDs like `Taipei Standard Time`) are silently skipped so the chain continues to the next step.

**Why skip rather than reject?** The chain is a best-effort resolution. An LLM client that passes `UTC+8` (a common LLM mistake) should still get a sensible timezone from the header or env rather than a hard error. Errors are reserved for the case where the caller *explicitly intends* a value to be used (e.g., the final resolved timezone is invalid — but that can't happen because step 4 always yields `"UTC"`).

### Validation semantics

- Only IANA keys accepted by Python's `zoneinfo.ZoneInfo()` are valid
- Offset strings and Windows timezone IDs are rejected at every step
- At `task.create.v1` time the resolved timezone is stored in `jobs.timezone NOT NULL`; this column is the single source of truth for the `RecurringJobWatcher`

### Forwarding: `MCP_USER_TZ`

The `MCP_USER_TZ` environment variable is set by the host process (or the harness) and forwarded into the agent container at all 4 phases (plan, implement, review, merge). This mirrors how `MCP_USER_ID` works and lets a developer's workstation timezone propagate into recurring job schedules without any changes to the client.

### W3 upgrade path: ALB OIDC header

Step 2 (`X-Timezone` header) is currently unused in W1/W2 (no header parsing in the MCP HTTP entrypoint). In W3, the ALB will be configured to enrich requests with OIDC-derived claims. At that point the HTTP handler can read the `X-Timezone` header from the request and pass it to `resolve_timezone` as `header_value`. **No changes to `resolve_timezone` itself will be required** — the chain already has the slot.

This mirrors the `user_id` resolver pattern (ADR-015 §W3 upgrade path), which similarly defers ALB header parsing to W3.

---

## Consequences

### Positive
- Zero-friction for callers who don't supply a timezone (they get UTC)
- Operator can set a default timezone per deployment via env without touching client code
- ALB header slot is reserved; W3 upgrade is a one-line change in the HTTP entrypoint
- Chain is fully unit-testable; each step is independently verifiable

### Negative / Trade-offs
- Silent skip of invalid values means a mis-typed timezone (e.g., `UTC+8`) falls through rather than erroring immediately at the tool boundary; the LLM may not notice
- Windows timezone IDs (common in Windows-native MCP clients) are unsupported; users on Windows must supply IANA keys or rely on the env step
