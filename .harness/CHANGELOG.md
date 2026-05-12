# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Per-issue git worktree isolation.** Each `-Issue N` invocation now operates
  in its own working directory at `.harness/worktrees/issue-N/`. The container
  bind-mounts this worktree instead of the whole repo, so multiple slices can
  run in parallel without trampling each other's in-flight files. The previous
  "single shared working tree" model caused real cross-slice contamination
  (uncommitted files from an interrupted slice leaked into the next one).
- **Per-issue lock files** at `.harness/locks/issue-N.lock` (JSON: pid +
  branch + phase + acquired_at + machine). A second terminal trying the same
  issue is rejected with the holder's diagnostic. Stale locks (dead PID) are
  auto-taken with a warning.
- **`-Cleanup N`** flag — removes the worktree + releases the lock for issue
  `N` without running any phase. Use when a slice was abandoned upstream.
- **`-Force`** flag — overrides live-PID lock conflicts and unstaged-changes
  guards. Use only when you know what the other process is doing.
- `lib/worktree.ps1` — `New-IssueWorktree`, `Resume-IssueWorktree`,
  `Remove-IssueWorktree`, `Test-IssueWorktreeExists`,
  `Get-IssueWorktreePath`, `Get-IssueWorktreeList`. Pester-tested with
  scriptblock injection.
- `lib/issue-lock.ps1` — `Invoke-AcquireIssueLock`,
  `Invoke-ReleaseIssueLock`, `Read-IssueLock`, `Test-PidAlive`,
  `Get-IssueLockPath`, `Get-IssueLockList`. Pester-tested.

### Changed
- `prompts/implement.md`: added "Turn-budget discipline" and "Execution
  discipline" sections. Captures hard-won lessons from real failure modes
  (lost WIP at max_turns, fixture-passes-but-suite-fails, postgresql.TIMESTAMPTZ
  AttributeError). Implementers now must commit incrementally and exercise
  the full test/migration paths before marking COMPLETE.
- `prompts/review.md`: added "Turn-budget discipline" and "Factual discipline"
  sections. Reviewers must commit each fix immediately (no batching), and
  must verify file-existence / test-result / diff claims via `git ls-files`
  / actual exit codes / `git diff` rather than reasoning from spec text.
- `run.ps1` docker invocations now set `UV_PROJECT_ENVIRONMENT=/tmp/venv`
  so a host's Windows `.venv` (with `.exe` shims) doesn't get bind-mounted
  into a Linux container and break `uv run`. Each docker run builds its
  own ephemeral Linux venv via `uv sync` on demand.

### Fixed
- `lib/format-event.ps1`: handle the new `rate_limit_event` shape
  (`{status, resetsAt, rateLimitType, overageStatus, ...}` without
  `utilization` / `surpassedThreshold`). Old format triggered StrictMode
  property-not-found at startup.
- `lib/parse-plan.ps1`: normalise `alternatives` / `blocked` collections
  to `@()` when JSON has them as `null`. Reject non-array values with a
  clear error.
- `lib/scan-deconflict.ps1`: wrap `gh pr list` JSON in `@()` to coerce
  single-result to array; use `PSObject.Properties` for resilient field
  access (avoids StrictMode property-not-found when `headRefName` is absent).
- `run.ps1` plan parser: switch from `$parsed.Error` / `$parsed.Plan`
  (dot-notation, throws under StrictMode on missing keys) to
  `ContainsKey` + subscript. Same fix for `alt`/`blocked` field access.

### Removed
- `lib/branch-claim.ps1` and `tests/branch-claim.Tests.ps1` —
  `Invoke-BranchClaim` is fully superseded by `Invoke-AcquireIssueLock` +
  `New-IssueWorktree`. The bash sibling `lib/branch-claim.sh` is intentionally
  retained — `run.sh` still consumes it. A future change can refactor the
  bash side the same way.

### Migration notes
- Existing projects using `run.ps1` will auto-adopt worktrees on the next
  `-Issue N` invocation; no manual migration needed for the agent flow.
- Local branches created under the old model continue to work but won't
  have a corresponding worktree. To migrate an in-progress branch: commit
  its work, `git branch -d <branch>`, then re-run with `-Issue N` to get
  a fresh worktree on a new branch from the same base.
- `.harness/worktrees/` and `.harness/locks/` are added to `.gitignore` —
  they are runtime state, never source.

## [0.1.1] — 2026-05-11

Portability fixes — the harness now actually works in projects whose
default branch is not `main`, and the bash runner is no longer a stub.

### Fixed
- `prompts/merge.md`: replaced hardcoded `main` with `{{TARGET_BRANCH}}` in
  `git log`, `gh pr create --base`, and the hard-rules section. Projects whose
  default branch is `master` / `develop` / `trunk` now merge correctly.
- `run.ps1`: pass `TARGET_BRANCH` to implement and merge substitutions
  (it was only computed inside the review block). Default branch is now
  resolved once via `git symbolic-ref refs/remotes/origin/HEAD` and reused
  across all three phases.
- `run.sh` implement path was effectively broken: it passed only
  `ISSUE_NUMBER=` while `prompts/implement.md` uses `{{ISSUE}}` + 7 other
  placeholders, all of which were silently stripped to empty strings. It also
  skipped branch claim, leaving the agent to commit on whatever branch HEAD
  happened to be. Now mirrors the PS path: derives the slug from
  `gh issue view`, claims the branch via the new `lib/branch-claim.sh`,
  computes target branch, and renders the prompt with the full substitution
  set. The container also gets `--permission-mode bypassPermissions`,
  `--model`, `--max-turns`, and `GH_TOKEN` forwarded — without these the
  bash container hung on permission prompts and had no `gh` auth.
- `prompts/implement.md` & `prompts/review.md`: dropped the `.sandcastle/`
  directory from "do not touch" lists — it leaked from the upstream source
  and confuses consumers whose project has no such directory.
- `prompts/implement.md`: replaced the literal `<default-branch>` comment
  string with a real `{{TARGET_BRANCH}}` substitution.
- `config.yml.example`: commented out the default `tests.block: pytest .`
  so a fresh clone does not assume a Python project. The example values stay
  in comments for users to uncomment.

### Added
- `lib/branch-claim.sh`: bash port of `lib/branch-claim.ps1`. Honors
  `BRANCH_CLAIM_LIST_CMD` / `_CREATE_CMD` / `_CHECKOUT_CMD` env overrides for
  test injection, matching the PS scriptblock contract.
- `run.sh` now accepts `--resume` to retry an existing claimed branch after
  a rate-limit interruption, mirroring PS `-Resume`.

## [0.1.0] — 2026-05-11

First tagged release. Stable enough to drop into another project as `.harness/`.

### Added
- Four-phase pipeline: `plan → implement → review → merge` (PowerShell).
  Bash runner covers `plan + implement`.
- Docker-based runner (`Dockerfile`) — Node 22 + Python 3 + git + gh + claude CLI,
  user `agent` (UID 1000).
- Config-driven model + max-turn selection per phase
  (`config.yml.example` → `config.yml`).
- Coding-standards injection into the review prompt
  (`CODING_STANDARDS.md.example` → `CODING_STANDARDS.md`).
- Atomic branch-claim via `git checkout -b` for safe parallel terminals.
- `-Resume` flag for rate-limit recovery without losing partial commits.
- Hooks: `before-tests.sh`, `after-implement.sh` (host-side, around implement phase).
- Per-issue logs split into human-readable + raw JSON
  (`logs/issue-{N}.log`, `logs/issue-{N}.jsonl`).
- Heartbeat counter advances during long runs so the operator sees progress.
- Smoke-test prompt that validates plumbing without spending agent tokens.
- Pester (`*.Tests.ps1`) and bats (`*.bats`) coverage for every `lib/` module.

### Notes
- `image: agent-harness:latest` in `config.yml.example` is intentional for
  local development. Pin to an immutable tag for production
  (e.g. `agent-harness:0.1.0`).
- Claude CLI and `gh` CLI inside the Docker image are installed from upstream
  at build time and are not version-pinned. Re-pin in your fork if you need
  bit-for-bit reproducibility.

[Unreleased]: https://github.com/PaynePew/kanban-harness/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/PaynePew/kanban-harness/releases/tag/v0.1.1
[0.1.0]: https://github.com/PaynePew/kanban-harness/releases/tag/v0.1.0
