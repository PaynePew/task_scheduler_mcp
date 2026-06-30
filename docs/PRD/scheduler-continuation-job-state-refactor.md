# PRD — Scheduler continuation chaining + `Job.state` lifecycle

**Source decisions:** ADR-067 (continuation chaining replaces pre-armed `WAITING` runs), ADR-068 (`Job.state` machine replaces the never-cleared `active` boolean), CONTEXT.md §1/§4.
**Validated by:** a job-scheduler survey (deep-research), the project's reference design `design-chatgpt-tasks.pdf`, and a throwaway state-machine prototype (scenarios A–H, 8/8).

## Problem Statement

Two user-visible defects, plus the structural debt under them:

1. **Quota lockout.** A user who schedules tasks over time is eventually refused with `Quota exceeded: active_total_per_user` even though almost nothing is actually running — because **every task ever created keeps counting against the limit forever**, and a finished task **cannot be cancelled** to free its slot (`task.cancel` on a terminal job returns `INVALID_STATE`). The active-total cap was meant to bound *resident load* (what still consumes the box, per ADR-055), but the code treats it as a *lifetime* counter. The demo/operator account hit the wall live during verification (~47/50, a create was rejected). This silently contradicts the advertised "100/day" creation capability.

2. **Invisible LLM output.** A user who asks the assistant to "polish this text" through the operator-funded `llm_polish` (or `llm_summarize`) action sees the task reach `completed` but **never sees the polished text**. No MCP surface returns a run's `result` (it is the internal inter-handler data plane), yet README prompt #6 tells the user to "verify `task.status` shows `result.polished`" — a field that does not exist in any response.

3. **Structural debt (root cause of fragility).** The chaining control plane — pre-armed `WAITING` runs, `ChainWatcher` flipping them, recursive `arm`, flip-time slow-consumer drop — is the most complex and least-precedented part of the system (it caused the #227 → #234–237 arm-race saga). It is also inconsistent with the project's own recurring path, which already uses the simpler **continuation** model the reference design prescribes.

## Solution

- A task **stops counting against the limit the moment it is finished or cancelled** — only tasks that can still produce a run count. Scheduling frees up again automatically.
- The scheduler models a task's lifecycle **explicitly** — `active` / `completed` / `cancelled` — kept **separate** from whether a given execution *succeeded* or *failed* (that stays on the run).
- Chained tasks "just work" under **one rule shared with recurring schedules**: when an upstream run reaches a terminal status, the downstream run is **created then** (continuation), not pre-armed and flipped. The `WAITING` limbo state and the `ChainWatcher` process disappear.
- Exactly-once downstream creation is **guaranteed at the data layer** (a unique constraint), not by hoping a background consumer runs exactly once.
- Docs become honest: `llm_polish` / `llm_summarize` are presented as **chain steps** (their output feeds `slack_post` / `email_send`), never as standalone "show me the result," and the demo-prompt series is corrected and tightened.

## User Stories

1. As a user who schedules tasks regularly, I want finished one-shot tasks to stop counting against my active-task limit, so that I am never locked out of creating new tasks by my own history.
2. As a user, I want to cancel a recurring task and have it immediately stop counting against my limit, so that cancelling actually frees capacity.
3. As a user, I want a finished task to be distinguishable from a cancelled task, so that my task list tells a truthful story of what happened.
4. As a user, I want a task I cancelled while its run was mid-flight to show as `cancelled` even though that run finished, so that the lifecycle reflects my intent while the run's own outcome stays accurate.
5. As a user, I want `task.status` to show whether a run *succeeded* or *failed* independently of whether the schedule is `completed`, so that "completed" never misleads me into thinking a failed run succeeded.
6. As a user chaining "do A, then B, then C", I want each downstream to run after its upstream succeeds, so that the pipeline executes end to end.
7. As a user, I want a chained downstream that has not fired yet to still appear in my task list as `scheduled`, so that I can see the whole pipeline I set up.
8. As a user, I want `task.status` on a not-yet-fired downstream to tell me which job it is waiting on (`triggered_by`), so that I understand why it has not run.
9. As a user with a recurring digest that fans out to Slack and email, I want both downstreams created each cycle, so that every run delivers to both sinks.
10. As a user whose upstream fires faster than a downstream can finish, I want the overlapping tick dropped (not queued or double-run), so that I never get stale or duplicated deliveries.
11. As a user, I want a downstream created exactly once per upstream run even if the system retries internally, so that I never receive a duplicate Slack message or email.
12. As a user, I want cancelling an upstream to stop its not-yet-run downstreams, so that cancelling a pipeline actually stops the whole pipeline.
13. As a user asking the assistant to polish a rough note, I want the result usable — by chaining it to Slack/email — so that operator-funded polishing produces something I actually receive.
14. As a user reading the docs/examples, I want them to reflect what the system actually does, so that following a "verify" step never points me at a field that does not exist.
15. As the operator, I want per-user caps to bound *resident load* (active recurring + active total), so that one user cannot exhaust the single small box.
16. As the operator, I want the demo account unblocked without manual DB surgery, so that demos and interviews keep working after the fix ships.
17. As the operator, I want cancelling a recurring schedule to leave any in-flight run to finish by default, so that cancellation is safe and predictable.
18. As an interviewer reviewing the architecture, I want chaining to follow a recognizable, idiomatic model (continuation), so that "why not do the simpler thing?" has a clean answer.
19. As an interviewer, I want a recorded rationale for reversing the earlier pre-arm design, so that the change reads as deliberate, not accidental.
20. As a developer/agent, I want a single owner of run creation reacting to terminal events, so that "what causes a run to exist" is answerable in one place.
21. As a developer/agent, I want the external 5-status contract preserved, so that existing MCP clients keep working without a schema change.
22. As a developer/agent, I want the migration to backfill existing jobs into the new `state`, so that the quota query is correct from the first request after deploy.
23. As a developer/agent, I want the coding standard updated in the same branch as the code, so that review stops enforcing the obsolete pre-arm rules.
24. As a developer/agent, I want the previously-faked chain-recurring test rewritten to drive the real continuation path, so that green means verified, not disguised.

## Implementation Decisions

- **`Job.state` enum replaces `Job.active`.** `state ∈ { active, completed, cancelled }` (`paused` deferred, enum extensible). `completed` = *schedule exhausted, no more runs* — **not** "succeeded"; run success/failure stays on `JobRun.status`. (ADR-068)
- **Continuation run-creation.** A trigger-driven downstream run is created when its upstream run reaches a terminal status, via the single `RunMaterializer`, in the same step that already creates recurring successors. No run is pre-armed at upstream-creation time. (ADR-067)
- **Remove `WAITING`.** The internal status machine drops `WAITING` (8 → 7 states) and `wait_for_run_id`. `trigger_on_status` becomes a *create* predicate, not a *flip* predicate. (ADR-067; CONTEXT §2 updated in-branch.)
- **Unify consumers; delete `ChainWatcher`.** `RecurringJobWatcher` generalizes into one continuation consumer that, per terminal `RunEvent`, materializes the recurring successor and each matching downstream in one transaction under one cursor. (ADR-067)
- **Exactly-once via a DB unique constraint** on the run's cause — `(job_id, triggering_run_id)` for trigger-driven, `(job_id, scheduled_tick)` for recurring successors. A duplicate insert is a no-op; the `processed_by` cursor is retained only as an efficiency layer. Terminal events are processed in `event_id` order. (ADR-067, realizing the reference design's "idempotent inserts.")
- **`from_run_id` set at downstream creation** (the upstream terminal `run_id`); the executor's ADR-064 injection step is removed with `WAITING`.
- **Slow consumer = do not create** (skip + audited drop) when the downstream already has an executing run — replacing "arm unconditionally then drop at flip." (ADR-067)
- **Predicate miss = no run + lightweight audit** event/log (replacing today's `CANCELLED_BY_CHAIN_MISS` run). (ADR-067)
- **State transitions** (encoded by the validated prototype — the decision, not the implementation):

  ```text
  settle_check(job):                       # runs in the continuation consumer, on terminal events
    if job.state != active: return
    if job has any non-terminal run: return         # still resident load
    if job is recurring root: return                # stays active until cancel
    if job is trigger-driven and parent.state == active: return   # parent still produces upstream runs
    CAS job.state: active -> completed
    cascade settle_check to downstream jobs

  cancel(job):  CAS job.state active -> cancelled; leave in-flight run to finish; cascade settle downstreams
  terminal race:  one-shot completion vs cancel are both CAS-from-active; first commit wins, loser no-ops, both exit the active count
  ```

- **Quota counts `state`.** Containment becomes `COUNT(*) WHERE state='active'` (and `job_type='recurring'` for the ≤5 cap); the partial index becomes `WHERE state='active' AND job_type='recurring'`. `cancelled_at` leaves the quota predicate (remains an audit timestamp). (ADR-068, amends ADR-055)
- **Cancel decoupled from in-flight.** `task.cancel` sets `state=cancelled` (stops future runs) and leaves any in-flight `JobRun` to finish. A forceful terminate path is out of scope.
- **External 5-status contract unchanged.** External status is derived at the MCP boundary from `(Job.state, latest run)`; a not-yet-triggered downstream shows `scheduled` with empty `runs` and a `triggered_by: <job_id>` field. No `task.*` schema change. (ADR-067 §9, ADR-014)
- **Backfill migration.** Add `state`; backfill (cancelled→`cancelled`; recurring-not-cancelled→`active`; terminal one-shot→`completed`; pending/running→`active`; chained derived from parent/own-run); drop `active`; rebuild the partial index. Must run before the new quota query goes live.
- **Docs deliverable.** Update `CODING_STANDARDS.md` (the ADR-065 "Run creation & chaining" rules, the outbox consumer list, the `arm`/`WAITING` run-source vocabulary, anti-pattern #10, and the already-stale `has_live_run` → `has_executing_run`) **in the implementation branch**; reframe README/landing prompt #6 and the `llm_polish`/`llm_summarize` positioning; correct the demo-prompt series.

## Testing Decisions

A good test asserts **external behavior at the highest seam**, never implementation details, and drives the **real** path (no faked seams — anti-pattern #10). Integration tests use the real Postgres from the compose stack with a fresh engine per test.

- **Continuation consumer / `RunMaterializer`** (core seam) — integration: a terminal `RunEvent` materializes the recurring successor and each matching downstream; a redelivered terminal event does not double-create (unique constraint); a downstream with an executing run is skipped (slow consumer). Prior art / changes: **rewrite `test_chain_recurring.py`** to drive the real continuation path; fold/retire `test_chain_watcher.py`, `test_chain_watcher_flip.py`, `test_chain_concurrency.py` (ChainWatcher removed); evolve `test_recurring_watcher.py` and unit `test_run_materializer.py`.
- **`Job.state` + quota** — integration: create/run/cancel and assert `state` plus `active_total`/`active_recurring`; assert a terminal job leaves the active count; assert the cancel-mid-run and terminal-race outcomes. Prior art: `test_cancel.py`, `test_containment.py`, unit `test_quotas_containment.py`.
- **External-status derivation** — MCP boundary: `task.status` on a not-yet-fired downstream returns `scheduled`, empty `runs`, and `triggered_by`. Prior art: `test_status.py`, unit `test_status_mapping.py`, `test_status_error_surface.py`.
- **Backfill migration** — `test_alembic_migration.py`: seed old-shape rows (terminal jobs with `active=true`), run the migration, assert each is reclassified to the correct `state` and the quota count is correct.
- **Docs/prompts** — no runtime seam; verified by rendering the landing to PNG and reading it, and by reading the README diff.

## Out of Scope

- A user-facing **result-retrieval surface** (a `task.result.v1` tool or enriched resource). Decision B1: reframe `llm_polish`/`llm_summarize` as chain-only and fix the docs; adding a result surface is a separate, additive design.
- A **forceful "terminate in-flight run"** operation (cancel stays best-effort/graceful by default).
- A **`paused` state** and `task.pause` / `task.resume` (enum is left extensible).
- **Fan-in** (`from_run_ids`, one downstream ← many upstreams) — already deferred (ADR-040).
- **Swapping SQS for a Postgres-backed queue** and reducing the poller-process count — named as interview-defensible simplifications but a separate, larger effort.

## Further Notes

- Reverses the pre-arm control plane of ADR-065; ADR-067 records why ADR-065 rejected continuation on a self-imposed layering rule (ADR-033) rather than a technical necessity.
- The backfill remediates the live demo/operator account (~47/50) without manual DB surgery.
- CONTEXT.md §2 (remove `WAITING` from the internal machine) and §7 (rewrite chaining from pre-arm to continuation) are updated when the refactor lands, not before — the glossary keeps describing the as-built system until the code matches.
