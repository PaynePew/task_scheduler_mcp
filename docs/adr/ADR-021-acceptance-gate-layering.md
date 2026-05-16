# ADR-021 — Acceptance Gate Layering (L1/L2/L3/L4)

**Status:** Accepted  
**Date:** 2026-05-16  
**Author:** Implementation Agent (issue #46)

---

## Context

W1 used a single acceptance gate: a 6-step MCP Inspector click-through flow documented in `PROMPT.md`. W2 adds four new capabilities (recurring, chaining, resources, prompts) and the LLM bonus reinterpretation. A single manual click-through is insufficient as the only gate because:

1. **CI coverage gap** — manual flows are not executed on every PR. A regression in RecurringJobWatcher or ChainWatcher could go unnoticed between manual runs.
2. **Confidence gradient** — a human verifying 11 steps manually has different failure modes than automated code. Both matter; neither replaces the other.
3. **Demo video timing** — a recorded video is the highest-confidence artifact for portfolio/grading, but it benefits from the cloud deployment story (W3/W4) for maximum impact.

---

## Decision

W2 uses a **four-layer acceptance gate** with increasing cost and decreasing automation:

| Layer | Artifact | Who runs it | When | Coverage |
|-------|----------|-------------|------|----------|
| L1 | CI E2E test | CI on every PR | Automated | All 11 steps (code path) |
| L2 | Manual Inspector flow | Developer | Before merge | All 11 steps (observable) |
| L3 | Claude Desktop sanity check | Developer | After local setup | LLM integration + counts |
| L4 | 3-minute demo video | Developer | **Deferred to W4** | Full story |

---

## Layer Definitions

### L1 — CI E2E Test

**File:** `tests/integration/test_e2e_inspector_flow.py::test_w2_bonuses`

Extends the existing `test_e2e_inspector_flow` (6 steps) with 5 new steps:

- Step 7: Create recurring job, stamp first run terminal via direct watcher invocation, tick `RecurringJobWatcher.poll_once()`, assert second PENDING run spawned
- Step 8: Cancel recurring job via `task.cancel.v1`, tick watcher again, assert no third run spawned
- Step 9: Create A (immediate) + B (triggered on A's SUCCEEDED), complete A via real executor, tick `ChainWatcher.poll_once()`, assert B flips PENDING → worker completes B → `task.status.v1` returns "completed"
- Step 10: `resources/list` returns 2 entries + `resources/templates/list` returns 1 = 3 total; `resources/read tasks://list` payload filtered by `_E2E_USER`
- Step 11: `prompts/list` returns 2; `prompts/get setup_summary` with topic + schedule substitutes both into the template

**Key design choice:** Watcher logic is tested via direct function invocation (`poll_once()`), not by waiting for real cron clock time. This keeps CI deterministic and fast (no `time.sleep` calls).

**Run:** `uv run pytest -m integration tests/integration/test_e2e_inspector_flow.py::test_w2_bonuses`

### L2 — Manual MCP Inspector Flow

**File:** `docs/W2-VERIFICATION.md`

~11 click-through steps in the MCP Inspector browser GUI. Covers all W1 regression steps plus W2 capability checks. Expected duration: ~5 minutes.

Validates **observable behavior** that L1 tests as code paths: visible tool counts, actual JSON responses in the browser, resources and prompts UI tabs populating correctly.

### L3 — Claude Desktop Sanity Check

**Documented in:** `README.md` § "Verify with Claude Desktop"

```bash
claude mcp add task-scheduler \
  -- uv run python -m app.entrypoints.mcp_stdio
```

Verify:
- 🔨 icon shows 5 tools
- Resources tab shows 3 entries
- Prompts tab shows 2 entries
- Natural-language task description → Claude calls `task.create.v1` correctly

This layer is the concrete demonstration of ADR-019's "Connect a real LLM" reinterpretation. It requires no API key beyond a Claude Desktop subscription.

Expected duration: ~2 minutes.

### L4 — Demo Video (3 minutes) — **Deferred to W4**

A 3-minute screen recording showing end-to-end task scheduling from natural language to completed job via Claude Desktop.

**Why deferred:**

1. **Cloud deployment story is stronger.** W3 deploys to ECS Fargate with ALB and CloudWatch. A demo recorded against a cloud endpoint (real DNS, real dashboard) is a higher-quality portfolio artifact than one against localhost. Recording in W4 (post-W3 deployment) pairs the demo with observable cloud infrastructure.

2. **LLM CI cost concerns.** If L4 were automated (screen-recording in CI), it would require a real Claude Desktop session, API spend on every CI run, and brittle UI automation. This cost/reliability tradeoff is unacceptable for a PR gate.

3. **Sprint focus.** W2 sprint is for implementation, not media production. The L1/L2/L3 layers provide sufficient quality signals for the current phase.

---

## Consequences

- `test_w2_bonuses` is the only L1 artifact; it runs on every PR via the existing CI integration test suite.
- `docs/W2-VERIFICATION.md` is the L2 artifact; human reviewers are expected to run it before closing the W2 sprint.
- `README.md` documents the L3 sanity check; any developer can reproduce it in ~2 minutes.
- L4 is tracked as a W4 action item; no branch or file is created for it in W2.
- The acceptance gate is considered **complete at L3** for W2 grading purposes.

---

## Related

- ADR-018: no server-side LLM in W2
- ADR-019: LLM bonus via client LLM integration (L3 is where this is demonstrated)
- `docs/W2-VERIFICATION.md`: L2 artifact
- `README.md` § "Verify with Claude Desktop": L3 documentation
