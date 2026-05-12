#Requires -Version 7
<#
Per-issue isolated working tree management.

Each issue gets its own directory at .harness/worktrees/issue-<N>/ (the
name "worktree" is historical — the underlying mechanism is a local git
clone, not a `git worktree`).

Why clones, not git worktrees: the harness bind-mounts the working tree
into a Linux container at /workspace. Real git worktrees have a `.git`
*file* containing `gitdir: <host-path-to-main-repo>/.git/worktrees/...`
— that host path is meaningless inside the container, so any `git`
command inside the container fails with "not a git repository." Agents
have been observed running `git init` to "fix" it, which destroys the
worktree linkage and seeds an orphan-root history.

Local clones avoid this: `git clone <local-path> <dest>` produces a
self-contained `.git` directory with hardlinked objects (so disk
overhead is minimal). The clone is a normal git repo from any
viewpoint — host or container.

Git operations are injected as scriptblocks for testability — production
callers omit the injection params and get real `git` invocations.
#>

function Get-IssueWorktreePath {
    param(
        [Parameter(Mandatory)][string]$RepoRoot,
        [Parameter(Mandatory)][int]$IssueNumber
    )
    # Use forward slashes inside the harness; Join-Path handles separators
    # but git on Windows accepts both. Keep it consistent across the API.
    return Join-Path $RepoRoot ".harness/worktrees/issue-$IssueNumber"
}

function Test-IssueWorktreeExists {
    param(
        [Parameter(Mandatory)][string]$RepoRoot,
        [Parameter(Mandatory)][int]$IssueNumber
    )
    return Test-Path (Get-IssueWorktreePath -RepoRoot $RepoRoot -IssueNumber $IssueNumber)
}

function New-IssueWorktree {
    <#
    Create a new per-issue clone, with a fresh branch checked out off
    BaseBranch. Returns the working-tree absolute path.

    Mechanism (see file header for rationale):
      1. git clone <RepoRoot> <worktreePath>      (hardlinked objects)
      2. set origin to the parent repo's GitHub URL so push goes upstream
      3. git checkout -b <BranchName> <BaseBranch>

    Idempotent guard: if the target directory already exists, throws —
    caller should detect this and use Resume-IssueWorktree instead.
    #>
    param(
        [Parameter(Mandatory)][string]$RepoRoot,
        [Parameter(Mandatory)][int]$IssueNumber,
        [Parameter(Mandatory)][string]$BranchName,
        [string]$BaseBranch = 'origin/main',
        [scriptblock]$GitClone = { param($RepoRoot, $Path)
            # `--no-checkout` defers the working-tree population to the
            # explicit branch checkout below — keeps "clone to X" and
            # "be on branch Y" as separate, debuggable steps.
            & git clone --no-checkout $RepoRoot $Path 2>&1 | Out-Host
            if ($LASTEXITCODE -ne 0) { throw "git clone failed (exit $LASTEXITCODE)" }
        },
        [scriptblock]$GitGetOriginUrl = { param($RepoRoot)
            $url = & git -C $RepoRoot remote get-url origin 2>&1
            if ($LASTEXITCODE -ne 0) { throw "git remote get-url origin failed (exit $LASTEXITCODE): $url" }
            return "$url".Trim()
        },
        [scriptblock]$GitSetOriginUrl = { param($Path, $Url)
            & git -C $Path remote set-url origin $Url 2>&1 | Out-Host
            if ($LASTEXITCODE -ne 0) { throw "git remote set-url failed (exit $LASTEXITCODE)" }
        },
        [scriptblock]$GitCheckoutBranch = { param($Path, $Branch, $Base)
            & git -C $Path checkout -b $Branch $Base 2>&1 | Out-Host
            if ($LASTEXITCODE -ne 0) { throw "git checkout -b $Branch $Base failed (exit $LASTEXITCODE)" }
        }
    )

    $worktreePath = Get-IssueWorktreePath -RepoRoot $RepoRoot -IssueNumber $IssueNumber

    if (Test-Path $worktreePath) {
        throw "Worktree already exists at '$worktreePath'. Use Resume-IssueWorktree or remove first."
    }

    # Ensure parent directory exists (.harness/worktrees/)
    $parentDir = Split-Path $worktreePath -Parent
    if (-not (Test-Path $parentDir)) {
        New-Item -ItemType Directory -Path $parentDir -Force | Out-Null
    }

    # All scriptblock invocations are piped to Out-Host so any stdout
    # they emit (git progress, fixtures) is shown but does NOT leak into
    # this function's output stream — otherwise the returned path would
    # become an array and break string interpolation downstream
    # (e.g. docker --volume "${path}:/workspace").
    & $GitClone $RepoRoot $worktreePath | Out-Host

    $githubUrl = & $GitGetOriginUrl $RepoRoot
    if ($githubUrl) {
        & $GitSetOriginUrl $worktreePath $githubUrl | Out-Host
    }

    & $GitCheckoutBranch $worktreePath $BranchName $BaseBranch | Out-Host

    return $worktreePath
}

function New-IssueWorktreeFromRemoteBranch {
    <#
    Rehydrate a per-issue clone from an EXISTING remote branch — used when
    -StartPhase != implement and no local worktree exists (the implement
    phase already happened in a previous run that has since been cleaned
    up; the branch is still on origin).

    Differs from New-IssueWorktree in that no new branch is created — the
    existing `origin/<BranchName>` is checked out and tracked. Caller must
    have already verified the remote branch exists.

    Returns the working-tree absolute path.
    #>
    param(
        [Parameter(Mandatory)][string]$RepoRoot,
        [Parameter(Mandatory)][int]$IssueNumber,
        [Parameter(Mandatory)][string]$BranchName,
        [scriptblock]$GitClone = { param($RepoRoot, $Path)
            & git clone --no-checkout $RepoRoot $Path 2>&1 | Out-Host
            if ($LASTEXITCODE -ne 0) { throw "git clone failed (exit $LASTEXITCODE)" }
        },
        [scriptblock]$GitGetOriginUrl = { param($RepoRoot)
            $url = & git -C $RepoRoot remote get-url origin 2>&1
            if ($LASTEXITCODE -ne 0) { throw "git remote get-url origin failed (exit $LASTEXITCODE): $url" }
            return "$url".Trim()
        },
        [scriptblock]$GitSetOriginUrl = { param($Path, $Url)
            & git -C $Path remote set-url origin $Url 2>&1 | Out-Host
            if ($LASTEXITCODE -ne 0) { throw "git remote set-url failed (exit $LASTEXITCODE)" }
        },
        [scriptblock]$GitFetch = { param($Path)
            & git -C $Path fetch origin 2>&1 | Out-Host
            if ($LASTEXITCODE -ne 0) { throw "git fetch origin failed (exit $LASTEXITCODE)" }
        },
        [scriptblock]$GitCheckoutTracking = { param($Path, $Branch)
            # `git checkout <branch>` creates a local branch tracking
            # origin/<branch> when the remote ref exists. If it doesn't,
            # exits non-zero with "pathspec ... did not match" — we
            # surface that as the failure reason.
            & git -C $Path checkout $Branch 2>&1 | Out-Host
            if ($LASTEXITCODE -ne 0) {
                throw "git checkout $Branch failed (exit $LASTEXITCODE) — does origin/$Branch exist?"
            }
        }
    )

    $worktreePath = Get-IssueWorktreePath -RepoRoot $RepoRoot -IssueNumber $IssueNumber

    if (Test-Path $worktreePath) {
        throw "Worktree already exists at '$worktreePath'. Use Resume-IssueWorktree."
    }

    $parentDir = Split-Path $worktreePath -Parent
    if (-not (Test-Path $parentDir)) {
        New-Item -ItemType Directory -Path $parentDir -Force | Out-Null
    }

    & $GitClone $RepoRoot $worktreePath | Out-Host

    $githubUrl = & $GitGetOriginUrl $RepoRoot
    if ($githubUrl) {
        & $GitSetOriginUrl $worktreePath $githubUrl | Out-Host
    }

    # Fetch defensively in case the local source repo didn't have the
    # branch cached. After clone+set-url, fetch goes to GitHub.
    & $GitFetch $worktreePath | Out-Host

    & $GitCheckoutTracking $worktreePath $BranchName | Out-Host

    return $worktreePath
}

function Resume-IssueWorktree {
    <#
    Validate an existing per-issue clone for resume. Returns the path if
    healthy; throws if missing or broken.

    Healthy means the directory exists AND has a `.git` (either the
    directory we created via clone, or — for back-compat with pre-clone
    harness versions — the worktree pointer file). Either is fine; both
    indicate the clone/worktree linkage is intact.
    #>
    param(
        [Parameter(Mandatory)][string]$RepoRoot,
        [Parameter(Mandatory)][int]$IssueNumber
    )

    $worktreePath = Get-IssueWorktreePath -RepoRoot $RepoRoot -IssueNumber $IssueNumber

    if (-not (Test-Path $worktreePath)) {
        throw "Cannot resume: no worktree at '$worktreePath'. Use New-IssueWorktree to create."
    }

    $gitEntry = Join-Path $worktreePath '.git'
    if (-not (Test-Path $gitEntry)) {
        throw "Worktree at '$worktreePath' is broken (no .git). Remove and recreate."
    }

    return $worktreePath
}

function Remove-IssueWorktree {
    <#
    Remove a per-issue clone directory. Returns $true if removed, $false
    if nothing to remove.

    With the clone-based model, the parent repo has no registration for
    this directory — a plain recursive delete is enough. We keep the
    -Force switch + injected scriptblock for back-compat with callers
    that pass them; both are accepted but `Remove-Item -Recurse -Force`
    handles the actual delete regardless of -Force (cloned working trees
    can have uncommitted changes the user wants to keep, but the higher-
    level cleanup flow has already decided to discard).
    #>
    param(
        [Parameter(Mandatory)][string]$RepoRoot,
        [Parameter(Mandatory)][int]$IssueNumber,
        [switch]$Force,
        [scriptblock]$RemoveDirectory = { param($Path)
            Remove-Item -Path $Path -Recurse -Force -ErrorAction Stop
        }
    )

    $worktreePath = Get-IssueWorktreePath -RepoRoot $RepoRoot -IssueNumber $IssueNumber
    if (-not (Test-Path $worktreePath)) {
        return $false
    }

    try {
        & $RemoveDirectory $worktreePath | Out-Host
    } catch {
        throw "Failed to remove worktree directory '$worktreePath': $_"
    }
    return $true
}

function Get-IssueWorktreeList {
    <#
    Returns the list of issue numbers that currently have worktrees on disk
    under .harness/worktrees/issue-*. Used by parallel-coordination logic
    and `-Cleanup` operations.
    #>
    param(
        [Parameter(Mandatory)][string]$RepoRoot
    )
    $worktreesDir = Join-Path $RepoRoot '.harness/worktrees'
    if (-not (Test-Path $worktreesDir)) { return @() }

    $issues = @()
    Get-ChildItem -Path $worktreesDir -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match '^issue-(\d+)$' } |
        ForEach-Object { $issues += [int]$Matches[1] }
    return $issues
}
