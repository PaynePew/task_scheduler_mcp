# TASK

Review the code changes on branch `{{BRANCH}}` (target: `{{TARGET_BRANCH}}`) for issue **#{{ISSUE}}** and improve clarity, consistency, and maintainability **while preserving exact functionality**.

# CONTEXT

## Branch diff

```bash
git checkout {{BRANCH}}
git diff {{TARGET_BRANCH}}...{{BRANCH}}
```

## Commits on this branch

```bash
git log {{TARGET_BRANCH}}..{{BRANCH}} --oneline
```

## Issue intent

```bash
gh issue view {{ISSUE}}
```

## Domain references (load lazily, only when relevant)

- Domain glossary: `{{DOCS_CONTEXT}}` — flag drift from canonical terms
- ADR directory: `{{DOCS_ADR_DIR}}` — flag any change that contradicts a recorded decision

# REVIEW PROCESS

1. **Understand the change.** Read the diff and commit messages. What is the implementer solving? What does the issue's AC require?

2. **Check correctness first** (cheaper to fix than to refactor on top of a bug):
   - Does the implementation match the AC and PRD intent?
   - Are edge cases handled (empty inputs, error responses, network failures)?
   - Are new/changed behaviors covered by tests?
   - Any unsafe casts (`as any`, `// @ts-ignore`, `# type: ignore`) without inline justification?
   - Any unchecked nulls or swallowed errors?
   - Any injection risk, credential leakage, or hardcoded secrets?

3. **Then look for clarity wins**:
   - Unnecessary complexity, deep nesting, redundant abstractions
   - Names that don't match what the thing does
   - Comments that paraphrase obvious code (delete) — keep only WHY-comments
   - Nested ternaries — prefer `if/else` chains
   - Over-clever one-liners — prefer explicit code

4. **Maintain balance.** Do not:
   - Over-simplify to obscurity
   - Combine too many concerns into one function
   - Remove helpful abstractions
   - Refactor speculatively — only fix what is wrong now

5. **Apply project standards** (substituted from `.harness/CODING_STANDARDS.md` — copied per-project from the bundled `.example` template — if present; otherwise empty):

{{CODING_STANDARDS_BLOCK}}

6. **Preserve functionality.** Never change WHAT the code does — only HOW. All original outputs and behaviors must remain intact. If a behavior change is needed, flag it for the human and do NOT make the change yourself.

# EXECUTION

If you find improvements to make:

1. Make changes directly on `{{BRANCH}}`.
2. Run tests + typecheck after each meaningful change.
3. **Commit immediately after each fix** with a `refactor:` Conventional-Commits prefix and a clear message. One logical change per commit. **Do NOT batch multiple fixes into one final commit** — see "Turn-budget discipline" below; this is the rule reviewers most reliably violate.

If the code is already clean and well-structured, do nothing.

# TURN-BUDGET DISCIPLINE (CRITICAL — read carefully)

The wrapper enforces a maximum number of tool-call turns. **Uncommitted edits on disk are invisible to the orchestrator** — if you run out of turns mid-flow, every fix you wrote but didn't commit is lost. This has actually happened on this project (review-2 wrote a complete, ruff-clean type-annotation fix and ran out of turns at the `git commit` step).

Apply these rules without exception:

1. **Commit each fix the moment it's clean.** Workflow: identify one issue → edit → run ruff/tests → `git add` + `git commit` → next issue. Never accumulate multiple fixes hoping to commit them as a batch at the end.

2. **If you find more issues than you have budget for, FLAG them instead of fixing.** It is far better to:
   - Make 2 fixes that get committed + flag 3 issues for the human
   
   than to:
   - Edit 5 fixes in the working tree, run out of turns at commit step, lose all 5.
   
   When in doubt, prefer the "Concerns flagged for human" section over making the change.

3. **Reserve turns for the closing report.** The `gh issue comment` step at the end is mandatory and takes 1–2 turns. Stop new fix work well before you exhaust the budget.

4. **No `git commit --amend`, no rebase.** Each fix is its own commit — this is also how the orchestrator can resume cleanly if your run gets cut short partway through.

# FACTUAL DISCIPLINE (CRITICAL — read carefully)

Your review report becomes the authoritative human-facing summary of this branch's state. Wrong claims in the report — especially about what files exist, what tests pass, or what the diff contains — mislead the human reviewer and can cause incorrect merge decisions. This has actually happened: a previous review reported `app/actions/base.py / echo.py / registry.py` as "existing but incomplete" because it had read those filenames in the slice spec — but those files were not actually on the branch.

Before making any claim about the working tree state, **verify it against the working tree, not against memory, not against the issue body, not against spec documents**:

1. **Claims about file existence MUST be verified via `git ls-files <path>` or `ls <path>` first.**
   - ❌ Bad: "The existing `app/actions/{base,echo,registry}.py` scaffolding is incomplete but out of scope."
     (Source of the error: the slice spec or another issue's body mentioned these files. They do not exist on this branch.)
   - ✅ Good: Run `git ls-files app/actions/` first. If only `__init__.py` is tracked, say: "`app/actions/` currently contains only an empty `__init__.py`; the handler / registry files referenced in slice S03's spec are not yet present on this branch."

2. **Claims about test results MUST cite the command and the exit code, and you MUST run both suites before claiming COMPLETE.**
   - `uv run pytest -m "not integration"` (unit)
   - `uv run pytest -m integration` (integration — Postgres + ElasticMQ are available at `host.docker.internal` and the relevant `DATABASE_URL`/`QUEUE_URL` env vars are pre-set; the harness host-side `before-tests.sh` brought the stack up before this container started)

   The single most common failure mode for past PRs has been: unit green, integration red, ship to CI, agent comes back to fix. Running integration here closes that loop. If integration fails and the diff is responsible, fix it (within turn budget) or flag it in "Concerns" — never silently omit the integration result from the report.

   If the venv was broken and you could not actually execute tests, say *that* — never summarise what you *expect* the tests would have done.

3. **Claims about the diff MUST be verified via `git diff` / `git show`.** Don't describe what the implementer *probably* did based on the issue body — look at what they actually committed.

4. **If you cannot verify a claim, omit it.** A shorter report with high-confidence claims is more valuable than a long report with mixed-confidence claims. The "Concerns flagged for human" section should not contain speculation.

The spec describes the target state; the working tree is the actual state. Your job is to compare the two, not to confuse them.

# COMPLETION

When done, post a structured review comment then exit:

```bash
gh issue comment {{ISSUE}} --body-file - <<'EOF'
## Review report

**Branch:** {{BRANCH}}
**Status:** COMPLETE

### Changes made
<!-- list of refactor commits, or "none" -->

### Concerns flagged for human
<!-- correctness or scope issues not safely fixed by this agent -->

### Test results
<!-- pass/fail counts -->

### Standards drift
<!-- rules violated but not fixed, with file:line references -->
EOF
```

Output `<promise>COMPLETE</promise>` and exit.

# HARD RULES

- Do NOT push, do NOT modify `{{TARGET_BRANCH}}`, do NOT close the issue, do NOT touch `.harness/` or `.claude/`.
- Do NOT introduce new features or expand scope. Flag anything missing for the human.
- Do NOT rewrite history (`git rebase`, `git commit --amend` are forbidden). Add new commits only.
