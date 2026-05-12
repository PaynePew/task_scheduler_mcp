# ADR-001: Project scope — Challenge Track from scratch + bonuses + AWS deployment

- **Status**: Accepted
- **Date**: 2026-05-12
- **Source**: doc/session/grilling-state.md Q1

## Context

The course assignment offers two tracks (Easy and Challenge) and a list of bonus features. This is a portfolio project intended to land a Backend/Infra role; the question is how ambitious to be given a 1-month full-time budget.

## Decision

Pursue the **Challenge Track from scratch**, ship **all listed bonuses** (recurring, chaining, LLM actions, MCP resources, MCP prompts), and **deploy to AWS** end-to-end. Budget: 4 weeks full-time, organised as W1 prototype → W2 bonuses → W3 AWS lift-and-shift → W4 polish.

## Alternatives considered

- **Easy Track only** — finishes in days but produces nothing that demonstrates distributed-systems thinking; weak resume signal.
- **Challenge without AWS** — saves W3 + W4 effort, but the role we target explicitly asks for AWS experience.
- **Challenge + partial bonuses** — leaves the most resume-relevant work (LLM actions, observability) on the table.

## Consequences

- High learning load; mitigated by sequencing (W1 spine before W2 features).
- Every architectural decision must hold up to the resume-narrative test: "can I defend this for 10 minutes in an interview?"
- Out-of-scope work is explicit (see PRD § Out of Scope) to keep the 4-week budget honest.
