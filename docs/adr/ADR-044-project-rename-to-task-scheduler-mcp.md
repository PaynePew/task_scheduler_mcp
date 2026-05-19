# ADR-044 — Project Rename: `chatgpt_task` → `task_scheduler_mcp`

**Date:** 2026-05-19  
**Status:** Accepted  
**Deciders:** PaynePew

---

## Context

The project was originally created as a course-assignment artifact named `chatgpt_task`. That name carries two problems:

1. **Vendor lock-in framing.** `chatgpt` implies the scheduler only works with OpenAI / ChatGPT, contradicting the LLM-agnostic stance established in ADR-018-amended. The scheduler exposes an MCP surface that any MCP-capable client can consume — Claude Desktop, Cursor, or any future client — regardless of the underlying LLM.

2. **Course-artifact stigma.** `chatgpt_task` reads as a homework assignment. The project has grown into a self-hostable, portfolio-quality MCP server with a multi-week sprint history, ADR-backed design decisions, and a live VPS deployment. The name should reflect that ambition.

ADR-018-amended (LLM-agnostic stance reaffirmed in W4) made the rename blocking: shipping W4 features under a ChatGPT-branded name would directly contradict the documented decision.

---

## Decision

Rename the project from `chatgpt_task` / `chatgpt-task-scheduler` to `task_scheduler_mcp` / `task-scheduler-mcp` across all layers:

| Layer | Old | New |
|---|---|---|
| GitHub repo | `PaynePew/chatgpt_task` | `PaynePew/task_scheduler_mcp` |
| Python package (`pyproject.toml`) | `chatgpt-task-scheduler` | `task-scheduler-mcp` |
| Container image | `ghcr.io/paynepew/chatgpt_task` | `ghcr.io/paynepew/task_scheduler_mcp` |
| VPS deploy path | `/opt/chatgpt_task` | `/opt/task_scheduler_mcp` |
| Terraform project tag | `chatgpt-task` | `task-scheduler-mcp` |
| ECR repo / RDS default names | `chatgpt-task` / `chatgpt_task` | `task-scheduler-mcp` / `task_scheduler_mcp` |
| CI lock-table / state-bucket prefix | `chatgpt-task-terraform-locks` | `task-scheduler-mcp-terraform-locks` |
| Backup R2 bucket | `chatgpt-task-backups` | `task-scheduler-mcp-backups` |
| Better Stack monitor | `chatgpt_task` | `task-scheduler-mcp` (manual via dashboard) |

**What does NOT change:**

- MCP tool namespace (`task.*`) — already vendor-neutral and part of the external API contract.
- MCP resource scheme (`tasks://`) — same.
- Internal Python package (`app`) — already generic.
- Database schema, table names, column names — all generic.
- Domain name `scheduler.paynepew.dev` — already neutral.
- Existing Docker image tags in `ghcr.io` storage — old tags remain indefinitely (public packages are free) for backward compatibility.

---

## Alternatives Considered

### Keep `chatgpt_task`, accept the stigma

Rejected. ADR-018-amended explicitly reaffirms the LLM-agnostic stance. Shipping W4 under a ChatGPT-branded name would be a direct contradiction in documentation.

### Rename to `mcp_scheduler` or `scheduler_mcp`

Considered. `task_scheduler_mcp` was preferred because it is more descriptive (domain = task scheduling, protocol = MCP) and less generic than `mcp_scheduler` (which could refer to any MCP server).

### Two-phase rename (code first, GitHub repo later)

Rejected. The GitHub repo rename is the canonical change; deferring it creates a window where code and repository identity diverge. GitHub's automatic redirect means old URLs keep working, so the risk of the rename itself is low.

---

## Consequences

**Positive:**
- Project name accurately describes what it does and which protocol it speaks.
- No ChatGPT / OpenAI vendor association — works with any MCP client.
- Resume / portfolio framing: name communicates the technical stack at a glance.

**Negative / mitigations:**
- Old GitHub URL (`github.com/PaynePew/chatgpt_task`) returns a 301 redirect — existing links keep working.
- Developers who cloned the old repo need to update their remote URL: `git remote set-url origin https://github.com/PaynePew/task_scheduler_mcp.git`
- VPS operators must move the deploy directory: `mv /opt/chatgpt_task /opt/task_scheduler_mcp` and update `.env` if it contains absolute paths.
- Terraform state S3 bucket and DynamoDB lock table names are defined per-account in secrets/variables — only the *default* values in `variables.tf` were updated; live infrastructure is unaffected until a Terraform apply is run with updated inputs.

**No impact on:**
- Running application code, database schema, or live traffic — the rename is purely identity/infrastructure.
