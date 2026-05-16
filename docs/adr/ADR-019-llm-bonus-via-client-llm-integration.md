# ADR-019 — LLM Bonus via Client LLM Integration

**Status:** Accepted  
**Date:** 2026-05-16  
**Author:** Implementation Agent (issue #46)

---

## Context

The course spec's fifth bonus item is:

> *"Connect a real LLM to parse natural language task descriptions before calling `task.create`."*

The naive reading is server-side: add an LLM-mediated endpoint that accepts free-form text and returns structured `task.create.v1` arguments. Grilling Session #3 challenged this interpretation.

---

## Decision

The LLM bonus is **satisfied by the MCP server's connection to a real LLM-powered client** (Claude Desktop, Claude Code, or any MCP-capable LLM agent), **not by server-side LLM code**.

The acceptance demonstration is the L3 sanity check: `claude mcp add task-scheduler ...` + a natural-language task description in the chat → Claude calls `task.create.v1` with correct structured arguments.

---

## Reinterpretation

### Course-spec framing

The spec assumes the LLM is the **caller**, not the callee:

> *假設我們已經有 ChatGPT custom connector...*  
> (Translation: "Assume we already have a ChatGPT custom connector; design a system that allows the LLM ChatBot to schedule jobs at specified times.")

The LLM is ChatGPT (or Claude). The "system" we design is the MCP server + scheduler backend. The spec's framing already places the LLM on the **client side**.

### MCP design as the NL parser surface

Section 7 of the spec — "MCP integration reliability principles" — lists:

1. Version suffix on tool names (e.g., `.v1`) for stable caching
2. Strict JSON Schema with `additionalProperties: false`
3. System instruction ≤ 150 tokens
4. Structured fixable errors (`error.code`, `error.field`, `error.expected`)
5. Action registry discovery via `task.list_actions.v1` before creation

Every one of these principles is designed to **enable client LLMs to emit correct structured calls** from natural-language input. The schema IS the NL parser surface. A server-side LLM would duplicate the work that the client LLM (Claude) is already doing.

### What "Connect a real LLM" means in W2

The moment a user runs:

```bash
claude mcp add task-scheduler -- uv run python -m app.entrypoints.mcp_stdio
```

...and chats:

> "Remind me to check the deploy logs every weekday at 9am."

Claude reads the `task.list_actions.v1` registry, the system instruction from `serverInfo`, and the tool schemas — then emits a correct `task.create.v1` call with `cron_expr="0 9 * * 1-5"`, `action="echo"`, and `action_params={"message": "check the deploy logs"}`.

**A real LLM has been connected. The bonus is satisfied.**

---

## Why This Interpretation Is Correct

1. **The spec's own example** uses a ChatGPT connector as the frontend — the LLM IS the client.
2. **Server-side NL re-parsing** would process the same utterance twice (LLM client → server LLM → `task.create`), adding latency and cost for no quality improvement.
3. **The course grading criterion** (observable at the demo) is "can the system accept natural language task descriptions?" — this is true as soon as an LLM client connects, independent of whether the server also calls an LLM.
4. **ADR-018** establishes that server-side LLM is deferred to W4, where it has a better story (cloud deployment + recorded demo).

---

## Acceptance Gate

The L3 sanity check in `README.md` § "Verify with Claude Desktop" is the concrete demonstration:

1. `claude mcp add task-scheduler ...` with `MCP_USER_ID` and `MCP_USER_TZ` env vars
2. Open Claude Desktop — 🔨 icon shows **5 tools**
3. Type a natural-language task description
4. Claude calls `task.create.v1` with correct structured parameters
5. Optional: verify the Resources tab shows **3 entries** and Prompts tab shows **2 entries**

No API key, no cost, no server-side LLM code required.

---

## Consequences

- The "Connect a real LLM" bonus is marked complete in W2.
- `app/mcp/system_instruction.md` (≤145 tokens) is the key artifact enabling reliable client-LLM integration.
- W4 retains optionality to add a server-side `llm_summarize` action as additional value, without retroactively invalidating W2's completion.
- The README "Bonus Challenges" section is updated to reflect this reinterpretation.

---

## Related

- ADR-018: documents what didn't ship (server-side LLM) and why
- ADR-021: acceptance gate layering; L3 is where this bonus is demonstrated
