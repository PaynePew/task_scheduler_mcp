# ADR-018-amended: W4 Reconsidered — Stays LLM-Agnostic

- **Status**: Accepted (amends ADR-018)
- **Date**: 2026-05-19
- **Deciders**: PaynePew
- **Related**: ADR-018 (original), ADR-019 (LLM bonus via client-LLM integration)

## Context

ADR-018 (2026-05-16) deferred server-side LLM work to W4 as optional polish. W4 is now active and the decision is revisited in light of two 2026 developments:

1. **ChatGPT Tasks** launched (OpenAI's first-party scheduled-task feature). It handles simple "remind me" workflows natively, targeting the same casual user segment that a server-side NL parser would have served.

2. **LangChain / LlamaIndex dominance** in agentic orchestration. Any user sophisticated enough to want programmable multi-step scheduling is already using an orchestration framework that connects to their LLM of choice — not a bespoke server-side parser.

These shifts make the W4 server-side LLM path weaker as a differentiator, not stronger.

## Decision

**No server-side LLM is added in W4.** The project stays LLM-agnostic: the MCP schema is the NL parser surface (per ADR-019), and the client LLM does all natural-language mapping.

The differentiator is auditable persistence + typed action handlers + the open self-host model — not the NL parsing layer.

## Alternatives considered

| Option | Verdict |
|---|---|
| Add `llm_summarize` action (Anthropic SDK) | Rejected — duplicates work already done by client LLM; adds API key dependency to CI |
| Add `llm_chat` stateless endpoint | Rejected — same duplication; ChatGPT Tasks serves the casual segment better |
| Support streaming via SSE for LLM chains | Deferred — valid future direction once a concrete use case drives it |

## Consequences

- `pyproject.toml` does not gain `openai` or `anthropic` packages in W4.
- The "no server-side LLM" stance becomes a deliberate architectural identity, not a temporary deferral.
- The project's competitive position against ChatGPT Tasks is **specialisation**: typed action handlers (Slack, GitHub, email, R2, calendar), auditable JobRun history, and self-hostable persistence that survives chat sessions.
- ADR-019 remains the canonical explanation of how the LLM bonus is satisfied without server-side code.
