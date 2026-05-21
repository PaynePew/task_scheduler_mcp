# ADR-060: Error-code vocabulary consolidation — map 4 drift codes back, keep MISSING_CONNECTION

- **Status**: Accepted
- **Date**: 2026-05-21
- **Deciders**: PaynePew
- **Source**: issue #157 (vocabulary-drift audit)
- **Related**: ADR-014 (MCP tool surface / envelope), ADR-051 (action tiering), ADR-057 (overload protection), ADR-058 (connection UX)

## Context

`CONTEXT.md §6` and ADR-014 define the canonical envelope error-code vocabulary as exactly six values:

```
USER_INPUT | NOT_FOUND | INVALID_STATE | UNKNOWN_ACTION | DUPLICATE | INTERNAL
```

`CODING_STANDARDS.md` requires: **"No new codes without an ADR."**

Five new codes landed in PRs #142/145/154–156 without amending ADR-014:

| Code | PR / ADR | Site |
|---|---|---|
| `OPERATOR_ONLY` | #142 / ADR-051 | `app/mcp/errors.py` |
| `OVERLOADED` | #145 / ADR-057 | `app/entrypoints/mcp_http.py` (2 sites) |
| `RATE_LIMITED` | #145 / ADR-057 | `app/entrypoints/mcp_http.py` |
| `BACKPRESSURE` | #145 / ADR-057 | `app/mcp/server.py` |
| `MISSING_CONNECTION` | #154–156 / ADR-058 | `app/mcp/server.py` |

## Options considered

1. **Map all five back to existing codes.** Minimal change, no vocabulary amendment.
2. **Formally add all five to ADR-014.** Widens the vocabulary to 11 codes.
3. **Hybrid.** Map four back; formally add only `MISSING_CONNECTION` because it carries a distinct structured payload (`connect_url`) that cannot be expressed without data loss by any existing code.

## Decision

**Option 3 (hybrid).**

### Codes mapped back to `INVALID_STATE`

- **`OPERATOR_ONLY`** (`errors.py`) — an operator-restricted action is a state-violation from the calling user's perspective: the system is in a state where that action is not permitted for them. `INVALID_STATE` already covers "tried to perform an action the system won't allow in the current state" (ADR-014: "Tried to cancel a terminal job, or a similar state-violation").
- **`OVERLOADED`** (`mcp_http.py` load-shedding + concurrency cap) — the server is temporarily in a state where it cannot accept requests.
- **`RATE_LIMITED`** (`mcp_http.py` per-user rate limit) — the user has reached a rate-limit state.
- **`BACKPRESSURE`** (`server.py` SQS queue-depth guard) — the system is in a high-load state; new task creation is temporarily blocked.

HTTP status codes (503, 429) already carry the precise machine-readable signal; the envelope `code` does not need to duplicate it.

### `MISSING_CONNECTION` formally added as 7th code

`MISSING_CONNECTION` is the only drift code with a structurally distinct payload: an optional `connect_url` field pointing the user to the connections dashboard (ADR-058). That field cannot be expressed by `INVALID_STATE` without the client having to guess which `expected`/`field` value signals "go connect". Keeping it as a separate code makes the envelope self-describing and allows LLM clients to branch on it deterministically.

## Consequences

- `CONTEXT.md §6` updated: vocabulary is now 7 codes.
- ADR-014 amended with the same change.
- `app/mcp/errors.py` `map_domain_error` docstring updated.
- All four mapped-back call sites updated in source.
- Existing tests for `OVERLOADED` / `RATE_LIMITED` updated to assert `INVALID_STATE`.
- Regression tests added: one per surface (operator gate, load-shedding, concurrency cap, rate limit, backpressure, missing connection).
