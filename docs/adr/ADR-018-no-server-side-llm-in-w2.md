# ADR-018 — No Server-Side LLM in W2

**Status:** Accepted  
**Date:** 2026-05-16  
**Author:** Implementation Agent (issue #46)

---

## Context

The course spec includes a fifth bonus item: *"Connect a real LLM to parse natural language task descriptions before calling `task.create`."* The original W2 plan (pre-Grilling Session #3) interpreted this as a server-side NL parser: a new `llm_summarize` action or an `llm_chat` endpoint that would accept free-form text, call OpenAI/Anthropic, and map the result to structured `task.create.v1` arguments.

W2 scope also originally anticipated server-side `llm_chat` and `llm_summarize` actions as Tier 1/2 work.

---

## Decision

**No server-side LLM is shipped in W2.** The LLM work is deferred to W4 as `(D-X)` optional polish.

The β path established in Grilling Session #3 reclassifies the LLM bonus: see ADR-019 for the full reinterpretation. This ADR records what **did not ship** and why.

---

## What Didn't Ship

| Feature | Original plan | Disposition |
|---------|--------------|-------------|
| `llm_summarize` action | Call OpenAI, return summary string | Deferred to W4 (D-X) |
| `llm_chat` action / endpoint | Accept free-form text, return structured tool arguments | Deferred to W4 (D-X) |
| Server-side NL → `task.create` mapping | Parse "remind me tomorrow at 9am" server-side | Not needed; see ADR-019 |
| OpenAI/Anthropic SDK dependency | `openai` or `anthropic` package | Not added to W2 |

---

## Rationale

### 1. Duplication of work already done by the client LLM

The course spec explicitly assumes a ChatGPT (or Claude) client is the caller:

> *假設我們已經有 ChatGPT custom connector, 請設計一個系統...*  
> (Translation: "Assume we already have a ChatGPT custom connector; design a system...")

When Claude Desktop calls `task.create.v1`, the NL-to-structured-args translation has **already happened** in the client LLM. A server-side LLM would re-parse the same intent, adding latency, cost, and a second external API dependency for zero benefit.

### 2. The MCP schema IS the NL parser surface

The entire tool design — strict JSON Schema, version suffix, ≤150-token system instruction, `error.expected` hints for self-correction — is engineered to make client LLMs succeed at structured generation without server-side assistance. Section 7 of the course spec (MCP integration reliability principles) is entirely about this contract.

### 3. CI cost and reliability

A server-side LLM would require an API key in CI, add $-per-run cost, and introduce flakiness from network timeouts and API rate limits. The test suite must be deterministic and free.

### 4. W4 is the right venue

W4 polish includes cloud deployment (ALB, ECS Fargate, CloudWatch). At that point, adding an `llm_summarize` action backed by a real LLM demonstrates end-to-end cloud integration — a stronger portfolio signal than a locally-tested stub. W4 also has budget for a recorded demo video where LLM-in-the-loop is clearly visible.

---

## What's Still Possible (W4 D-X)

- `llm_summarize` action: call Anthropic SDK, store summary in `action_params`, return to caller
- `llm_chat` action: stateless NL → structured args endpoint, demonstrated against deployed ALB
- Streaming responses via the SSE transport (already in the MCP HTTP layer)

None of these require schema changes — they are new action registrations.

---

## Connection to ADR-019

The LLM bonus is **satisfied** by W2's design via a reinterpretation. See ADR-019 for how "Connect a real LLM" is fulfilled without server-side LLM code.

---

## Consequences

- W2 ships 0 new external API dependencies.
- `pyproject.toml` does not gain `openai` or `anthropic` packages in W2.
- The `Bonus Challenges` section in README.md is updated to clarify that all five bonuses are implemented (the LLM bonus via client integration, not server-side parsing).
- W4 retains optionality to add server-side LLM without schema migrations.
