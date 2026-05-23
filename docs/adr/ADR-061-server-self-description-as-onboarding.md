# ADR-061: Server self-description is the onboarding first impression

- **Status**: Accepted
- **Date**: 2026-05-23
- **Deciders**: PaynePew
- **Source**: incident 2026-05-23 (daily-brief setup); parent PRD #196; slice #197
- **Related**: ADR-014 (MCP tool surface / envelope), ADR-049 (multi-tenant OAuth delegation), ADR-050 (dual credential model), ADR-051 (action surface tiering), ADR-058 (connection UX), ADR-060 (error code vocabulary consolidation)

## Context

The MCP `initialize` response includes an `instructions` field that every client (human or LLM) sees at handshake time. Until now, `app/mcp/system_instruction.md` was a hand-maintained text file with a stale capability summary:

```
Available actions: echo (test/reminder), http_call (POST/GET to URL).
```

The file was written when only two actions existed (W1 bootstrap) and never updated as the registry grew to 8 actions (W2-W4 added `slack_post`, `email_send`, `github_digest`, `llm_summarize`, `llm_polish`, `calendar_digest_ics`).

On 2026-05-23, the operator set up a daily-brief routine via Claude Desktop. Because the cold-start `instructions` only listed `echo` and `http_call`, the Claude LLM **never even called `task.list_actions.v1`**. It went straight to proposing Slack incoming webhooks and Resend (third-party email), as if the server didn't have these capabilities. The user had to manually correct: "I already set up OAuth at `/connections` — there should be a way to enable it." Only then did the LLM discover the truth.

The failure mode was not "user got a confusing error" — it was "server got zero telemetry because no tool call ever happened." A stale cold-start text caused the LLM to silently route around the server.

## Decision

Three commitments, captured here so future contributors don't repeat the regression:

### 1. `instructions` is a load-bearing onboarding surface, not a casual blurb

Treat the `instructions` string with the same care as a public API surface. Test it. Review changes to it. If it drifts from the registry, the system gets worse in a way that costs every new client one incident before they recover.

### 2. Source of truth is the action registry, not a hand-maintained text file

The `instructions` string is **composed at server startup** from `ACTION_REGISTRY`, not read from a static file alone. Each `ActionHandler` carries two new `ClassVar`s on the Protocol:

- `summary_line: ClassVar[str]` — one-liner (≤80 chars) injected into the actions block
- `required_provider: ClassVar[str | None]` — OAuth provider for the action, or `None`

The text file (`app/mcp/system_instruction.md`) carries the prose preamble + a `{ACTIONS_BLOCK}` placeholder. `build_system_instruction(registry, template)` is a pure function that fills the placeholder. Adding a new handler automatically surfaces it; the file no longer needs editing when the registry changes.

A pytest (`tests/unit/test_system_instructions.py`) enforces:
- every registered action's `name` appears in the composed string
- every OAuth-gated handler's `required_provider` is mentioned on its action line
- the `/connections` onboarding URL appears at least once
- the opening anti-substitution directive matches a stable regex

The regex is narrow enough to fail if the directive is removed but loose enough that the operator can re-word surrounding prose without breaking the test.

### 3. Reuse `MISSING_CONNECTION`; do not add `AUTH_REQUIRED`

The draft PRD initially proposed a new error code `AUTH_REQUIRED` for OAuth-gated failure. **Rejected.** ADR-060 fixed the vocabulary at 7 codes; `MISSING_CONNECTION` is already the canonical code for "OAuth missing" and already carries an optional `connect_url` field. Future Layer 3 work (push `MISSING_CONNECTION` from preflight into `execute()` paths) reuses the existing code.

## Layer 2 is deliberately deferred; Layer 3 is implemented

This ADR covers Layer 1 only. Layer 2 (`task_list_actions_v1` carries `auth_status`) is listed in the parent PRD (#196) as backlog and would warrant its own ADR.

**Layer 3 has been implemented** (issue #211). OAuth-gated handlers (`slack_post`, `github_digest`, `email_send`) now call `check_oauth_for_execute()` at the top of `execute()`, which:
- Skips the check in dev/CI (no KMS configured) so existing unit tests are unaffected.
- Loads the `OAuthConnection` row and inspects the plaintext `expires_at` column (no decryption needed).
- Returns `ActionResult(error_code="MISSING_CONNECTION")` on a missing row or an expired token with no refresher.
- Attempts a token refresh when a `refresher` is supplied; returns `MISSING_CONNECTION` only on refresh failure.
- Returns `None` on success so the handler falls through to the normal `get_token()` path.

`JobRun` gained an `error_code` column (migration `0009`). The worker executor writes `error_code` from the `ActionResult` into the run row. `task.status.v1` now surfaces a structured `{"code": ..., "message": ..., "connect_url": ...}` error block in its response when `error_code` is set, matching the shape already used by `task.create` preflight (ADR-058, ADR-060).

## Consequences

**Positive:**
- New actions become discoverable without manual `instructions` edits.
- The pytest pins the contract: drift is caught at test time instead of customer time.
- The Protocol additions (`summary_line`, `required_provider`) document a previously-implicit invariant.
- LLM clients have a structured cue (`(needs slack OAuth)` suffix) for routing users to `/connections#<service>`.

**Negative:**
- Adding an action now requires writing a one-line `summary_line` (small cost, large discoverability win).
- The prose preamble still requires human judgement — wording that successfully deters LLM substitution behavior is empirical, not derivable. Mitigated by the regex test pinning the imperative shape.

**Reversibility:** Low cost to revert. The Protocol additions are additive; deleting them would only break callers that hard-depend on the new attributes (i.e. this ADR's own changes).

## Layer 2 amendment (issue #210)

Layer 2 has been implemented. The contract is:

- **No new error codes.** The existing `MISSING_CONNECTION` envelope and 7-code vocabulary (ADR-060) are unchanged. This is purely additive metadata on the discovery surface.
- **Three new fields per action** in the `task.list_actions.v1` response: `auth_required` (bool), `auth_status` (`"connected" | "not_connected" | "n/a"`), and `connect_url` (string or null).
- **Batch-loaded.** A single `ConnectionStore.list()` call per `task.list_actions.v1` request fetches the user's full connection set; N registered actions do not produce N DB queries.
- **`_check_oauth_connection` preflight is unchanged.** `task.create.v1` still issues its own per-action token fetch as before (that path requires a valid token, not just presence of a row).
- **No separate `tasks://auth-status` resource was needed.** Inlining the fields into the existing action list is sufficient for LLM routing decisions and avoids adding a new resource endpoint.

## Open

Layers 1, 2, and 3 of the parent PRD (#196) are now implemented. Remaining backlog items would not invalidate the core decisions in this ADR:

- **S4 — end-to-end integration walk** (#212): scenario test that exercises discovery → preflight error → connect → success → expiry → execute() error → reconnect → success in a single test, pinning the cross-layer contract.
- **Token-refresh telemetry surfaced to the user** for recurring jobs whose connection expires between ticks. Would be a separate concern (notification side-channel, not a new error code).
- **`tasks://auth-status` MCP resource** as an alternative to the inline fields on `task.list_actions.v1`. Not currently needed; Layer 2's inline shape is sufficient. Belongs to its own ADR if revisited.
