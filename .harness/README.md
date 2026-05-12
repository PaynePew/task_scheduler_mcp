# Agent Harness — Operator Manual

A Docker-based runner that drives `claude` against a GitHub issue tracker using your **Claude subscription** (not an API key). One terminal, one issue at a time, four phases: plan → implement → review → merge.

**Heritage:** a simplified, single-operator take on the **sandcastle** harness pattern. We kept the four-phase loop and the Docker-bind-mount-per-phase model; dropped the daemon, shared queue, and `state.json`. Use sandcastle if you need queues, dashboards, or multi-tenant orchestration. Use this if you want *one terminal, one issue, one PR*.

**Recommended pairing:** install [mattpocock/skills](https://github.com/mattpocock/skills) into your target project's `.claude/skills/` so the implement and review agents have battle-tested skills available during runs.

**Design rationale:** lives in the ADR/PRD of the repo that hosts this harness. <!-- TODO: link your own PRD / ADR here -->

---

## Prerequisites

| Requirement | Check |
|---|---|
| Docker Desktop running | `docker info` |
| `gh` CLI logged in | `gh auth status` |
| `claude` CLI installed | `claude --version` |
| OAuth token obtained | `claude setup-token` |

**Getting the OAuth token:**

```powershell
# One-time setup:
claude setup-token
# Copy the printed token, then either:
$env:CLAUDE_CODE_OAUTH_TOKEN = '<token>'
# or drop it into .harness/.env.local (gitignored):
# CLAUDE_CODE_OAUTH_TOKEN=<token>
```

The token is a long-lived value from your Claude subscription account. It reaches the container via environment variable — never embedded in a `docker run` argument — so it does not appear in the host process listing.

---

## Install into your project

This repo is the harness *source*. It is designed to live at **`.harness/`** inside your target project. Pick one integration style:

### Option A — git submodule (recommended; versioned, easy upgrades)

```powershell
git submodule add https://github.com/PaynePew/kanban-harness.git .harness
git -C .harness checkout v0.1.0    # pin to a release tag
git submodule update --init --recursive
```

Upgrade later with `git -C .harness fetch --tags && git -C .harness checkout v0.2.0`.

### Option B — git subtree (vendor in-tree, no submodule pointers)

```powershell
git remote add harness https://github.com/PaynePew/kanban-harness.git
git fetch harness --tags
git subtree add --prefix=.harness harness v0.1.0 --squash
```

### Option C — plain clone (simplest, not version-locked)

```powershell
git clone https://github.com/PaynePew/kanban-harness.git .harness
Add-Content .gitignore ".harness/"
```

### Then, regardless of option

```powershell
Copy-Item .harness/config.yml.example         .harness/config.yml
Copy-Item .harness/CODING_STANDARDS.md.example .harness/CODING_STANDARDS.md
Copy-Item .harness/.env.local.example          .harness/.env.local
# edit all three for your project; .env.local and the two copies are gitignored
```

> **Why `.harness/`?** All scripts resolve paths relative to themselves (`$PSScriptRoot`), so the directory name is not load-bearing — but every example in this README assumes `.harness/`. Keeping the name consistent across projects also makes it easy to grep for harness invocations.

---

## First run

```powershell
# Windows / PowerShell
pwsh ./.harness/run.ps1 -SmokeTest
```

```bash
# Linux / macOS / CI
./.harness/run.sh --smoke-test
```

A successful smoke test prints `PONG` from inside the container, confirming that Docker, the OAuth token, `gh` auth, and the image are all wired correctly. The smoke test costs no agent tokens.

If the image does not exist yet, the runner builds it automatically from `Dockerfile` and caches the hash in `.harness/.image-hash`. Subsequent runs skip the build unless `Dockerfile` changes.

---

## Four-phase pipeline

```
host (Windows / *nix)
│
│  ┌────────────────────────────────────────────────────────┐
│  │  run.ps1 / run.sh (bare)                               │
│  │                                                         │
│  │  ① PLAN      ─── claude run ──▶ ranked issue list      │
│  │                                  "Run #N? [Y/n]"        │
│  └────────────────────────────────────────────────────────┘
│
│  ┌────────────────────────────────────────────────────────┐
│  │  run.ps1 -Issue N                                      │
│  │                                                         │
│  │  ② IMPLEMENT ─── claude run ──▶ claims branch          │
│  │                                  writes code + tests    │
│  │                                  commits                │
│  │                                                         │
│  │  ③ REVIEW    ─── claude run ──▶ reads diff             │
│  │                                  refactors              │
│  │                                  commits refactor:      │
│  │                                                         │
│  │  ④ MERGE     ─── claude run ──▶ git push -u origin     │
│  │                                  gh pr create --fill    │
│  └────────────────────────────────────────────────────────┘
│
│  branch ready for human review on GitHub
```

Each phase runs in a fresh container with the repo bind-mounted as `/workspace`. Phases share state through git commits on the feature branch — no daemon, no shared queue, no `.harness/state.json`.

---

## Bare `run` flow

```powershell
pwsh ./.harness/run.ps1
```

1. Runs the **plan phase**: scans open issues, deconflicts against branches already claimed (`{branch_prefix}{N}-*`) and open PRs, ranks the remainder.
2. Prints the top candidate and alternatives.
3. Prompts: `Run #N? [Y/n]`
4. On confirmation, runs **implement → review → merge** on that issue.

```bash
# Linux / macOS equivalent (plan + implement only; review/merge PS-only in v1)
./.harness/run.sh
```

---

## Flag reference

### PowerShell (`run.ps1`)

| Flag | Description |
|---|---|
| *(bare)* | Plan → confirm → implement → review → merge |
| `-Plan` | Plan phase only; print ranking, exit. No implement. |
| `-Yes` | Plan + auto-confirm top candidate + full pipeline. No Y/n prompt. |
| `-Issue N` | Skip plan. Claim + implement + review + merge issue N. |
| `-Resume` | Resume implement on an existing branch for `-Issue N`. Fails if no matching branch exists. |
| `-SkipReview` | Skip the review phase after implement. Branch is ready to push manually. |
| `-SkipMerge` | Skip the merge phase after review. No push, no PR created. |
| `-SmokeTest` | Run the smoke-test prompt only (validates plumbing). |
| `-PlanModel <id>` | Override `agents.plan.model` from config. |
| `-ImplementModel <id>` | Override `agents.implement.model` from config. |
| `-ReviewModel <id>` | Override `agents.review.model` from config. |
| `-MergeModel <id>` | Override `agents.merge.model` from config. |
| `-PlanMaxTurns N` | Override `agents.plan.max_turns` from config. |
| `-ImplementMaxTurns N` | Override `agents.implement.max_turns` from config. |
| `-ReviewMaxTurns N` | Override `agents.review.max_turns` from config. |
| `-MergeMaxTurns N` | Override `agents.merge.max_turns` from config. |

### Bash (`run.sh`)

| Flag | Description |
|---|---|
| *(bare)* | Plan → confirm → implement |
| `--plan` | Plan phase only, print ranking, exit. |
| `--yes` | Plan + auto-confirm top candidate + implement. |
| `--issue N` | Skip plan, implement issue N. |
| `--smoke-test` | Validate plumbing only. |

---

## Manual issue selection from another terminal

If you want to run a specific issue without going through the plan phase:

```powershell
# Terminal A — already running plan on something else
# Terminal B — claim and implement a specific issue directly
pwsh ./.harness/run.ps1 -Issue 42
```

The plan phase deconflicts against local branches and open PRs, so a second terminal that runs plan will never pick an issue another terminal has claimed. If you skip plan (`-Issue N` directly), parallel-safety is enforced by a per-issue lock file at `.harness/locks/issue-N.lock`: a second terminal trying the same issue is rejected with the holder's PID + branch + phase. Each issue also gets its own `git worktree` at `.harness/worktrees/issue-N/`, so two terminals working on *different* issues never see each other's in-flight files.

Recovery flags:
- `-Resume`           — attach to an existing worktree+branch for the issue (lock auto-takes from the dead PID of the previous run).
- `-Force`            — override a live-PID lock (only use if you really know what the other process is doing).
- `-Cleanup N`        — remove the worktree + lock for issue `N` without running any phase. Use when a slice was abandoned (PR closed, branch deleted upstream).

---

## Resume after rate-limit

When `claude` exits non-zero with `Rate limit exceeded` or `usage_limit_exceeded` in the log, the wrapper surfaces the exact resume command:

```
Run interrupted. Resume with:
  pwsh ./.harness/run.ps1 -Issue 30 -Resume
```

Partial commits on the branch are preserved. `-Resume` skips branch creation and continues from the last committed state.

```powershell
pwsh ./.harness/run.ps1 -Issue 30 -Resume
```

---

## Config

Copy `.harness/config.yml.example` to `.harness/config.yml` and edit. The real `config.yml` is gitignored — each project keeps its own. Required keys:

```yaml
image:          agent-harness:latest
branch_prefix:  kanban-issue          # or feat-, issue-, anything consistent
tracker:
  type:         github
  repo:         <your-org>/<your-repo>
```

Optional:

```yaml
defaults:
  model:        claude-sonnet-4-6

agents:
  plan:
    model:      claude-opus-4-7
    max_turns:  10
  implement:
    model:      claude-sonnet-4-6
    max_turns:  80
  review:
    model:      claude-opus-4-7
    max_turns:  30
  merge:
    model:      claude-sonnet-4-6
    max_turns:  20

docs:
  context:      CONTEXT.md
  prd_dir:      docs/prd
  adr_dir:      docs/adr

# Replace with your project's actual test / typecheck commands.
tests:
  block:        <your test command>      # e.g. `pytest .`, `go test ./...`, `cargo test`

typecheck:
  block:        <your typecheck command> # e.g. `tsc --noEmit`, `mypy .`

commit:
  style:        "Conventional Commits (feat/fix/test/docs/chore/refactor)"
```

CLI model flags (e.g. `-ImplementModel`) override config values for that run only.

---

## Troubleshooting

### Pre-flight failures

The wrapper checks prerequisites before starting any agent. Common failures:

| Error | Fix |
|---|---|
| `CLAUDE_CODE_OAUTH_TOKEN is not set` | Run `claude setup-token` and export the token, or add it to `.harness/.env.local`. |
| `gh auth status` fails | Run `gh auth login` on the host. |
| Docker daemon not running | Start Docker Desktop. |
| `Missing config key: image` | Check `.harness/config.yml` for required keys. |
| Branch `kanban-issueN-*` already exists | Another terminal already claimed the issue. Use `-Resume` to continue it, or pick a different issue. |

### Hooks not firing

Hooks are bash scripts in `.harness/hooks/` that run **on the host**, not inside the container. The wrapper invokes them at fixed lifecycle points around the implement phase:

| Hook | Fires |
|---|---|
| `before-tests.sh` | Before the implement container starts (skipped for smoke-test runs). |
| `after-implement.sh` | After the implement container exits (success or failure). |

If a hook did not fire:

1. Confirm the script lives at `.harness/hooks/<name>.sh` and matches the expected name above.
2. Confirm the script is readable by the user running the wrapper. Hooks are invoked via `bash <path>`, so the executable bit is not required, but bash must be on `PATH`.
3. Check the wrapper's stdout for the line `WARNING: hook '<name>' exited <code>` — non-zero hook exits do not abort the run, only warn.
4. Hooks receive context via environment variables: `HARNESS_ISSUE`, `HARNESS_BRANCH`, `HARNESS_PHASE`. Read these inside the script — no positional arguments are passed.

### Log file location

| Log | Path |
|---|---|
| Implement run | `.harness/logs/issue-{N}.log` |
| Plan run | `.harness/logs/plan-{timestamp}.log` |
| Smoke test | `.harness/logs/smoke-test.log` |

`.harness/logs/` is gitignored except for `.gitkeep`. Logs persist between runs and are overwritten on each new run for the same issue number.

---

## Cost / rate-limit reality (Pro subscription)

- Pro has a **5-hour rolling message window**. A full four-phase run (plan + implement + review + merge) on a complex slice can consume 30–50% of the window.
- **One issue at a time.** Opening two terminals for the same issue will exhaust the rate limit before either finishes. Open a second terminal only after the first has committed its implement phase and the window budget allows it.
- If a run crashes mid-way, partial commits stay on the local branch. Use `-Resume` to continue — the agent reads the branch state and picks up from the last commit.
- The harness does not sleep, estimate remaining budget, or auto-retry. It surfaces the failure and prints the resume command. The operator decides when to resume.

---

## Files

| Path | Purpose |
|---|---|
| `Dockerfile` | Node 22 + Python 3 + git + gh + claude CLI; user `agent` (UID 1000). |
| `config.yml` | Per-project config (image tag, branch prefix, tracker, models, test commands). Copy from `config.yml.example`. |
| `CODING_STANDARDS.md` | Injected into the review prompt as `{{CODING_STANDARDS_BLOCK}}`. Copy from `CODING_STANDARDS.md.example`. |
| `run.ps1` | Entry point — Windows/PowerShell. Full four-phase pipeline. |
| `run.sh` | Entry point — Linux/macOS/CI. Plan + implement. |
| `lib/*.{ps1,sh}` | Pure-function modules mirrored across PS and bash. |
| `prompts/{plan,implement,review,merge,smoke-test}.md` | Project-agnostic prompt templates with `{{KEY}}` substitution. |
| `tests/` | Pester (`.Tests.ps1`) and bats (`.bats`) coverage for every `lib/` module. |
| `.env.local` | OAuth token override (gitignored). Copy from `.env.local.example`. |
| `logs/` | Per-run container stdout (gitignored except `.gitkeep`). |
| `CHANGELOG.md` | Versioned release notes. Pin downstream consumers to a tag. |
| `LICENSE` | MIT. See file for upstream attribution to sandcastle. |
