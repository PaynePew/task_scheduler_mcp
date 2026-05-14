#Requires -Version 7
<#
.SYNOPSIS
    Generic Docker agent harness entry point for Windows / PowerShell.
.PARAMETER Plan
    Run the plan phase only; print ranked candidates and exit.
.PARAMETER Yes
    Auto-confirm the top candidate from the plan phase and chain into the
    implement phase (skip the Y/n prompt and the manual `-Issue` follow-up).
.PARAMETER SmokeTest
    Run the smoke-test prompt (validates plumbing without spending agent tokens).
.PARAMETER Issue
    Issue number. Skips plan and runs the implement agent directly.
.PARAMETER Resume
    Resume implement on an existing branch for the given -Issue. Fails if no
    matching branch exists. Cannot be used without -Issue.
.PARAMETER StartPhase
    Which phase to start at: implement (default), review, or merge. When set
    to review or merge, the earlier phases are skipped and the worktree is
    rehydrated from the existing remote branch if it doesn't exist locally.
    Use when an earlier run completed implement (and possibly review) but a
    later phase hit max-turns, was killed, or otherwise needs a clean retry
    without re-running everything. Requires -Issue.
.PARAMETER SkipReview
    Skip the review phase after implement. The branch can still be merged.
.PARAMETER SkipMerge
    Skip the merge phase after review. Commits and review stay on the branch; no push, no PR.
.PARAMETER PlanModel
    Override agents.plan.model from config (CLI takes precedence over config).
.PARAMETER ImplementModel
    Override agents.implement.model from config.
.PARAMETER ReviewModel
    Override agents.review.model from config.
.PARAMETER MergeModel
    Override agents.merge.model from config.
.PARAMETER PlanMaxTurns
    Override agents.plan.max_turns from config.
.PARAMETER ImplementMaxTurns
    Override agents.implement.max_turns from config.
.PARAMETER ReviewMaxTurns
    Override agents.review.max_turns from config.
.PARAMETER MergeMaxTurns
    Override agents.merge.max_turns from config.
.EXAMPLE
    pwsh ./.harness/run.ps1               # plan → confirm → implement → review → merge
    pwsh ./.harness/run.ps1 -Plan         # plan only, print ranking, no implement
    pwsh ./.harness/run.ps1 -Yes          # plan + auto-confirm + implement + review + merge
    pwsh ./.harness/run.ps1 -Issue 30     # skip plan, claim + implement + review + merge #30
    pwsh ./.harness/run.ps1 -Issue 30 -Resume
    pwsh ./.harness/run.ps1 -Issue 30 -SkipReview
    pwsh ./.harness/run.ps1 -Issue 30 -SkipMerge
    pwsh ./.harness/run.ps1 -Issue 30 -StartPhase review  # skip implement, run review + merge
    pwsh ./.harness/run.ps1 -Issue 30 -StartPhase merge   # skip implement + review, run merge only
    pwsh ./.harness/run.ps1 -SmokeTest
    pwsh ./.harness/run.ps1 -Issue 30 -ImplementModel claude-haiku-4-5 -ImplementMaxTurns 40
#>
[CmdletBinding()]
param(
    [switch]$Plan,
    [switch]$Yes,
    [switch]$SmokeTest,
    [int]$Issue,
    [switch]$Resume,
    [ValidateSet('implement', 'review', 'merge')]
    [string]$StartPhase = 'implement',
    [switch]$SkipReview,
    [switch]$SkipMerge,
    [switch]$Force,
    [int]$Cleanup,
    [string]$PlanModel,
    [string]$ImplementModel,
    [string]$ReviewModel,
    [string]$MergeModel,
    [int]$PlanMaxTurns,
    [int]$ImplementMaxTurns,
    [int]$ReviewMaxTurns,
    [int]$MergeMaxTurns
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$HarnessRoot = $PSScriptRoot
$RepoRoot    = Split-Path $HarnessRoot -Parent
# Default docker bind-mount target. Issue-bound flows override this to a
# per-issue git worktree path so multiple slices can run in parallel without
# cross-contaminating each other's working trees.
$mountPath   = $RepoRoot

. "$HarnessRoot/lib/load-config.ps1"
. "$HarnessRoot/lib/render-prompt.ps1"
. "$HarnessRoot/lib/image-cache.ps1"
. "$HarnessRoot/lib/heartbeat.ps1"
. "$HarnessRoot/lib/parse-plan.ps1"
. "$HarnessRoot/lib/scan-deconflict.ps1"
. "$HarnessRoot/lib/format-event.ps1"
. "$HarnessRoot/lib/worktree.ps1"
. "$HarnessRoot/lib/issue-lock.ps1"

# ── Crash-safe lock cleanup ───────────────────────────────────────────────────
# The Issue-mode flow acquires a per-issue lock and (on success) releases it
# in the cleanup block near the end of the script. If a terminating error
# fires between those two points (docker spec error, ForEach pipeline error,
# Ctrl+C, etc.), the explicit release never runs and the lock leaks — and
# because Windows pwsh PIDs survive script termination, the next run sees a
# "live" stale lock and demands -Force.
#
# This trap covers that gap: any terminating error releases the held lock
# (if any) before re-throwing. The script's normal cleanup path clears
# $script:HeldLockIssue back to 0 after its own release, so a clean exit
# leaves nothing for the trap to do.
$script:HeldLockIssue = 0
trap {
    if ($script:HeldLockIssue -ne 0) {
        Write-Host ''
        Write-Host "  Terminating error — releasing lock for issue #$($script:HeldLockIssue)" -ForegroundColor Yellow
        try {
            Invoke-ReleaseIssueLock -RepoRoot $RepoRoot -IssueNumber $script:HeldLockIssue | Out-Null
        } catch {
            Write-Warning "  Lock release in trap failed: $_"
        }
        $script:HeldLockIssue = 0
    }
    # Re-throw the original error so the script still exits non-zero.
    break
}

# ── Terminal rendering helpers ─────────────────────────────────────────────────

$script:AnsiOk = $false
try { $script:AnsiOk = [bool]$Host.UI.RawUI.SupportsVirtualTerminal } catch {}
$script:HbVisible = $false
$script:HbStartTime = $null

function Write-RunHeader([string]$IssueLabel, [string]$Model, [string]$Branch, [string]$LogFile) {
    Write-Host "Issue: $IssueLabel  Agent: $Model  Branch: $Branch  Log: $LogFile" -ForegroundColor Cyan
}

function Start-HbTimer { $script:HbStartTime = Get-Date }

function Write-HbLine([hashtable]$State) {
    $line = if ($script:HbStartTime) {
        Format-HeartbeatLine -State $State -StartTime $script:HbStartTime -Now (Get-Date)
    } else {
        Format-HeartbeatLine -State $State
    }
    if ($script:AnsiOk -and $script:HbVisible) {
        Write-Host "`e[1A`e[2K$line"
    } else {
        Write-Host $line
    }
    $script:HbVisible = $true
}

function Close-HbLine([string]$Msg, [string]$Color = 'Green') {
    if ($script:AnsiOk -and $script:HbVisible) {
        Write-Host "`e[1A`e[2K$Msg" -ForegroundColor $Color
    } else {
        Write-Host $Msg -ForegroundColor $Color
    }
    $script:HbVisible = $false
}

function Format-Exclusions([System.Collections.Generic.HashSet[int]]$Set) {
    if ($null -eq $Set -or $Set.Count -eq 0) { return 'none' }
    ($Set | Sort-Object | ForEach-Object { "#$_" }) -join ', '
}

# ── Helpers ────────────────────────────────────────────────────────────────────

# Writes a section header to the human-readable log file.
function Write-LogHeader {
    param(
        [Parameter(Mandatory)][string]$Phase,
        [Parameter(Mandatory)][string]$LogFile,
        [string]$RawLogFile = ''
    )
    $stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    $banner = @(
        "=================================================================="
        "  $Phase  ·  $stamp"
        "=================================================================="
        ""
    ) -join "`n"
    Set-Content -Path $LogFile -Value $banner -Encoding UTF8
    if ($RawLogFile) {
        Set-Content -Path $RawLogFile -Value '' -Encoding UTF8
    }
}

# Writes a footer pointing to the raw log file (if any).
function Write-LogFooter {
    param(
        [Parameter(Mandatory)][string]$LogFile,
        [string]$RawLogFile = ''
    )
    if ($RawLogFile) {
        $rel = $RawLogFile.Replace($RepoRoot, '').TrimStart('\','/').Replace('\','/')
        Add-Content -Path $LogFile -Value ""
        Add-Content -Path $LogFile -Value "  raw  → $rel"
    }
}

# Per-line dual-output writer: raw → .raw.jsonl, formatted → terminal + log.
# Non-JSON lines pass through unchanged to both files.
function Write-FormattedLine {
    param(
        [Parameter(Mandatory)][string]$RawLine,
        [Parameter(Mandatory)][string]$LogFile,
        [Parameter(Mandatory)][string]$RawLogFile
    )
    Add-Content -Path $RawLogFile -Value $RawLine
    try {
        $ev = $RawLine | ConvertFrom-Json -AsHashtable -ErrorAction Stop
        $formatted = Format-StreamEvent -Event $ev
        if ($formatted) {
            Write-Host $formatted
            Add-Content -Path $LogFile -Value $formatted
            Add-Content -Path $LogFile -Value ''
        }
    } catch {
        Write-Host $RawLine
        Add-Content -Path $LogFile -Value $RawLine
    }
}

function Invoke-HarnessHook {
    param(
        [string]$HookName,
        [string]$HooksDir,
        [int]$Issue,
        [string]$Branch,
        [string]$Phase
    )
    $hookPath = Join-Path $HooksDir $HookName
    if (-not (Test-Path $hookPath)) { return }
    $env:HARNESS_ISSUE  = "$Issue"
    $env:HARNESS_BRANCH = $Branch
    $env:HARNESS_PHASE  = $Phase
    # Convert the Windows path to a form `bash` can resolve. Two failure modes
    # are possible when PowerShell hands `bash` a `C:\...` argv:
    #   - Git Bash / MSYS2 silently strips the backslashes → bash sees
    #     `C:UsersMaxL...` and reports "No such file or directory" (exit 127).
    #   - WSL bash receives the path intact but doesn't recognize Windows drive
    #     syntax — it expects `/mnt/c/...`.
    # `wslpath -u` (present in WSL bash and Git Bash w/ WSL integration) handles
    # both cases; bare Git Bash falls back to plain forward-slash form.
    $bashPath = bash -c "wslpath -u '$hookPath' 2>/dev/null"
    if (-not $bashPath) {
        $bashPath = $hookPath -replace '\\', '/'
    }
    bash $bashPath
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  WARNING: hook '$HookName' exited $LASTEXITCODE — continuing." -ForegroundColor Yellow
    }
}

# Warns whenever any two phases share the same model — self-review safety is
# weakened when the same model produces both the change and the review of it.
function Invoke-SameModelWarning {
    param([Parameter(Mandatory)][hashtable]$Agents)

    $planModel      = $Agents.plan.model
    $implementModel = $Agents.implement.model
    $reviewModel    = $Agents.review.model
    $mergeModel     = $Agents.merge.model

    $pairs = @()
    if ($planModel      -eq $implementModel) { $pairs += 'plan == implement' }
    if ($implementModel -eq $reviewModel)    { $pairs += 'implement == review' }
    if ($reviewModel    -eq $mergeModel)     { $pairs += 'review == merge' }
    if ($planModel      -eq $reviewModel)    { $pairs += 'plan == review' }
    if ($planModel      -eq $mergeModel)     { $pairs += 'plan == merge' }
    if ($implementModel -eq $mergeModel)     { $pairs += 'implement == merge' }

    if ($pairs.Count -eq 0) { return }
    Write-Host ''
    Write-Host ("  WARNING: same-model phase pairs detected ($($pairs -join '; '))." `
        + ' Using the same model for multiple phases reduces reasoning diversity.' `
        + ' Proceeding anyway.') -ForegroundColor Yellow
}

function Fail([string]$Msg, [string]$Remedy = '') {
    Write-Host "ERROR: $Msg" -ForegroundColor Red
    if ($Remedy) { Write-Host "  Run: $Remedy" -ForegroundColor Yellow }
    # `exit` bypasses the script-level trap that would normally release a
    # held lock — release explicitly here so Fail() is symmetric with the
    # trap path. Idempotent: no-op if nothing's held or the lock file is
    # already gone.
    if ($script:HeldLockIssue -ne 0) {
        try {
            Invoke-ReleaseIssueLock -RepoRoot $RepoRoot -IssueNumber $script:HeldLockIssue | Out-Null
        } catch {
            Write-Warning "  Lock release in Fail failed: $_"
        }
        $script:HeldLockIssue = 0
    }
    exit 1
}

function Step([string]$Label) {
    Write-Host "── $Label " -ForegroundColor Cyan -NoNewline
    Write-Host ('─' * [Math]::Max(0, 50 - $Label.Length)) -ForegroundColor DarkGray
}

function Get-PromptHostPath([string]$MountPath) {
    # The container always reads /workspace/.harness/.current-prompt.md, which
    # maps to <bind-mounted-host-dir>/.harness/.current-prompt.md. In plan
    # mode that's $RepoRoot/.harness/; in issue mode it's the worktree's
    # .harness/ (an empty submodule mount point — we treat it as a plain
    # directory since the file is ephemeral and cleaned up in finally).
    $dir = Join-Path $MountPath '.harness'
    if (-not (Test-Path $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
    return Join-Path $dir '.current-prompt.md'
}

# ── Pre-flight checks ──────────────────────────────────────────────────────────

Step 'Pre-flight checks'

# 1. CLAUDE_CODE_OAUTH_TOKEN
$token = $env:CLAUDE_CODE_OAUTH_TOKEN
if (-not $token) {
    $envFile = "$HarnessRoot/.env.local"
    if (Test-Path $envFile) {
        foreach ($line in (Get-Content $envFile)) {
            if ($line -match '^CLAUDE_CODE_OAUTH_TOKEN=(.+)$') {
                $env:CLAUDE_CODE_OAUTH_TOKEN = $Matches[1].Trim()
                $token = $env:CLAUDE_CODE_OAUTH_TOKEN
                break
            }
        }
    }
}
if (-not $token) { Fail 'Missing CLAUDE_CODE_OAUTH_TOKEN.' 'claude setup-token' }

# 2. Docker daemon
docker info 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) { Fail 'Docker daemon not running. Start Docker Desktop and retry.' }

# 3. gh auth
gh auth status 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) { Fail 'Not authenticated with GitHub CLI.' 'gh auth login' }

# 4. GH_TOKEN — forwarded to container so the agent can run `gh` inside.
# Auto-populated from `gh auth token` when not pre-set in the shell.
if (-not $env:GH_TOKEN) {
    $env:GH_TOKEN = (& gh auth token 2>$null)
    if ($env:GH_TOKEN) { $env:GH_TOKEN = $env:GH_TOKEN.Trim() }
    if (-not $env:GH_TOKEN) { Fail 'gh is logged in but `gh auth token` returned empty.' 'gh auth refresh' }
}

# 4. git repo
if (-not (Test-Path "$RepoRoot/.git")) { Fail 'Not inside a git repository.' }

Write-Host '  All pre-flight checks passed.' -ForegroundColor Green

# ── Cleanup mode (early exit) ──────────────────────────────────────────────────
# -Cleanup <N>  removes the worktree + lock for issue #N and exits. Use when
# a slice was abandoned (PR closed without merge, branch deleted, etc.) and
# you need to free the issue number for re-claim.

if ($Cleanup) {
    Step "Cleanup issue #$Cleanup"
    $removed = $false
    try {
        $removed = Remove-IssueWorktree -RepoRoot $RepoRoot -IssueNumber $Cleanup -Force:$Force
        if ($removed) {
            Write-Host "  Worktree removed: $(Get-IssueWorktreePath -RepoRoot $RepoRoot -IssueNumber $Cleanup)" -ForegroundColor Green
        } else {
            Write-Host "  No worktree to remove." -ForegroundColor DarkGray
        }
    } catch {
        Fail "$_" "pwsh ./.harness/run.ps1 -Cleanup $Cleanup -Force"
    }
    $releasedLock = Invoke-ReleaseIssueLock -RepoRoot $RepoRoot -IssueNumber $Cleanup
    if ($releasedLock) {
        Write-Host "  Lock released: $(Get-IssueLockPath -RepoRoot $RepoRoot -IssueNumber $Cleanup)" -ForegroundColor Green
    } else {
        Write-Host "  No lock to release." -ForegroundColor DarkGray
    }
    exit 0
}

# ── Load config ────────────────────────────────────────────────────────────────

Step 'Loading config'
try {
    $configPath = "$HarnessRoot/config.yml"
    if (-not (Test-Path $configPath)) {
        $examplePath = "$HarnessRoot/config.yml.example"
        if (Test-Path $examplePath) {
            Fail "Missing $configPath." "cp $examplePath $configPath   # then edit tracker.repo etc."
        } else {
            Fail "Missing $configPath and no .example template found." "Create .harness/config.yml from scratch"
        }
    }
    $cfg = Import-HarnessConfig -ConfigPath $configPath
} catch {
    Fail "Config error: $_"
}
$imageName  = $cfg.image
$markerPath = "$HarnessRoot/.image-hash"
Write-Host "  image=$imageName  branch_prefix=$($cfg.branch_prefix)" -ForegroundColor DarkGray

# ── CLI overrides (CLI > config > built-in default) ────────────────────────────
if ($PlanModel)         { $cfg.agents.plan.model           = $PlanModel }
if ($ImplementModel)    { $cfg.agents.implement.model      = $ImplementModel }
if ($ReviewModel)       { $cfg.agents.review.model         = $ReviewModel }
if ($MergeModel)        { $cfg.agents.merge.model          = $MergeModel }
if ($PlanMaxTurns)      { $cfg.agents.plan.max_turns       = "$PlanMaxTurns" }
if ($ImplementMaxTurns) { $cfg.agents.implement.max_turns  = "$ImplementMaxTurns" }
if ($ReviewMaxTurns)    { $cfg.agents.review.max_turns     = "$ReviewMaxTurns" }
if ($MergeMaxTurns)     { $cfg.agents.merge.max_turns      = "$MergeMaxTurns" }

# ── Same-model warning (all phase pairs) ──────────────────────────────────────
Invoke-SameModelWarning -Agents $cfg.agents

# ── StartPhase validation ──────────────────────────────────────────────────────
# -StartPhase only makes sense for issue-bound runs. Catch the bad combo
# early so the user gets a useful message instead of weird downstream
# behavior (e.g. -StartPhase review during plan would try to rehydrate a
# worktree with no branch name yet derived).
if ($StartPhase -ne 'implement' -and -not $Issue) {
    Fail "-StartPhase $StartPhase requires -Issue <N>." `
         "pwsh ./.harness/run.ps1 -Issue <N> -StartPhase $StartPhase"
}

# ── Image cache check / rebuild ────────────────────────────────────────────────

Step 'Image cache check'
if (Test-ImageRebuildNeeded -DockerfilePath "$HarnessRoot/Dockerfile" -MarkerPath $markerPath -ImageName $imageName) {
    Write-Host "  Rebuilding image: $imageName" -ForegroundColor Yellow
    docker build -t $imageName -f "$HarnessRoot/Dockerfile" "$RepoRoot"
    if ($LASTEXITCODE -ne 0) { Fail 'docker build failed.' }
    Save-ImageHash -DockerfilePath "$HarnessRoot/Dockerfile" -MarkerPath $markerPath
    Write-Host '  Image built and hash cached.' -ForegroundColor Green
} else {
    Write-Host '  Image up-to-date — no rebuild needed.' -ForegroundColor Green
}

# ── Select prompt, claim branch, build substitutions ──────────────────────────

$branchName     = ''
$implementModel = ''
$maxTurns       = ''

if ($SmokeTest) {
    $promptFile = "$HarnessRoot/prompts/smoke-test.md"
    $logFile    = "$HarnessRoot/logs/smoke-test.log"
    $runLabel   = 'smoke-test'
    $subs       = @{}
} elseif ($Issue) {
    $promptFile = "$HarnessRoot/prompts/implement.md"
    $logFile    = "$HarnessRoot/logs/issue-$Issue.log"
    $runLabel   = "issue-$Issue"

    # Derive kebab slug from issue title (used to form the branch name)
    $issueTitle = gh issue view $Issue --repo $cfg.tracker.repo --json title --jq '.title' 2>&1
    if ($LASTEXITCODE -ne 0) { Fail "gh issue view #$Issue failed: $issueTitle" }
    $slug = ($issueTitle -replace '[^A-Za-z0-9]+', '-').ToLower().Trim('-')
    if ($slug.Length -gt 40) { $slug = $slug.Substring(0, 40).TrimEnd('-') }
    if (-not $slug) { Fail "Could not derive slug from issue #$Issue title: '$issueTitle'" }

    $branchName = "$($cfg.branch_prefix)$Issue-$slug"

    # Derive target branch (default branch of the repo) — also used as the
    # worktree base. Done early so it's available for New-IssueWorktree below.
    $targetBranch      = git -C $RepoRoot symbolic-ref refs/remotes/origin/HEAD --short 2>$null
    if (-not $targetBranch) { $targetBranch = 'origin/main' }
    $targetBranchShort = $targetBranch -replace '^origin/', ''

    # 1. Acquire the issue lock first. Refuses if another live process holds
    # it (re-run with -Force to override). Stale locks (dead PIDs) auto-take.
    Step 'Acquiring issue lock'
    try {
        $lockPath = Invoke-AcquireIssueLock `
            -RepoRoot     $RepoRoot `
            -IssueNumber  $Issue `
            -BranchName   $branchName `
            -Phase        'implement' `
            -Force:       $Force
    } catch {
        Fail "$_" "pwsh ./.harness/run.ps1 -Issue $Issue -Force"
    }
    # Arm the script-level trap to release this lock if a terminating
    # error happens before the normal cleanup block runs.
    $script:HeldLockIssue = $Issue
    Write-Host "  Lock: $lockPath" -ForegroundColor DarkGray

    # 2. Resolve the worktree: create fresh, or attach to an existing one for
    # -Resume. Each issue's worktree lives under .harness/worktrees/issue-N/
    # and is bind-mounted into the container instead of the whole repo, so
    # parallel slices can't trample each other's files.
    Step 'Resolving worktree'
    $worktreeExists = Test-IssueWorktreeExists -RepoRoot $RepoRoot -IssueNumber $Issue
    if ($StartPhase -ne 'implement') {
        # -StartPhase review|merge: use existing worktree if present, otherwise
        # rehydrate from the existing remote branch (implement was done in a
        # previous run that has since been cleaned up).
        if ($worktreeExists) {
            $worktreePath = Resume-IssueWorktree -RepoRoot $RepoRoot -IssueNumber $Issue
            Write-Host "  Resumed worktree: $worktreePath" -ForegroundColor Green
        } else {
            Write-Host "  No local worktree — rehydrating from origin/$branchName" -ForegroundColor DarkGray
            try {
                $worktreePath = New-IssueWorktreeFromRemoteBranch `
                    -RepoRoot     $RepoRoot `
                    -IssueNumber  $Issue `
                    -BranchName   $branchName
            } catch {
                Fail "Cannot rehydrate worktree for -StartPhase ${StartPhase}: $_" `
                     "pwsh ./.harness/run.ps1 -Issue $Issue   # (start fresh from origin/main)"
            }
            Write-Host "  Rehydrated worktree: $worktreePath" -ForegroundColor Green
        }
    } elseif ($Resume) {
        if (-not $worktreeExists) {
            Fail "No worktree for issue #$Issue to resume." "pwsh ./.harness/run.ps1 -Issue $Issue   # (start fresh, no -Resume)"
        }
        $worktreePath = Resume-IssueWorktree -RepoRoot $RepoRoot -IssueNumber $Issue
        Write-Host "  Resumed worktree: $worktreePath" -ForegroundColor Green
    } else {
        if ($worktreeExists) {
            Fail "Worktree for issue #$Issue already exists. Either resume it or clean up." "pwsh ./.harness/run.ps1 -Issue $Issue -Resume   # OR   pwsh ./.harness/run.ps1 -Cleanup $Issue"
        }
        $worktreePath = New-IssueWorktree `
            -RepoRoot     $RepoRoot `
            -IssueNumber  $Issue `
            -BranchName   $branchName `
            -BaseBranch   $targetBranch
        Write-Host "  Created worktree: $worktreePath" -ForegroundColor Green
    }
    Write-Host "  Branch: $branchName" -ForegroundColor Green

    # All subsequent docker runs (implement, review, merge) bind-mount this
    # worktree instead of $RepoRoot, isolating this slice's writes.
    $mountPath = $worktreePath

    $implementModel = $cfg.agents.implement.model
    $maxTurns       = $cfg.agents.implement.max_turns

    # Resolve substitution values from config (missing paths drop their line via render-prompt)
    $docsPrdDir     = if ($cfg.docs -is [hashtable])      { $cfg.docs.prd_dir }         else { '' }
    $docsContext    = if ($cfg.docs -is [hashtable])      { $cfg.docs.context }          else { '' }
    $docsAdrDir     = if ($cfg.docs -is [hashtable])      { $cfg.docs.adr_dir }          else { '' }
    $testsBlock     = Get-ConfigBlock -Config $cfg -Section 'tests'     -WorkDir $RepoRoot
    $typecheckBlock = Get-ConfigBlock -Config $cfg -Section 'typecheck' -WorkDir $RepoRoot
    $commitStyle    = if ($cfg.commit -is [hashtable])    { $cfg.commit.style }          else { '' }

    $subs = @{
        ISSUE           = "$Issue"
        BRANCH          = $branchName
        TARGET_BRANCH   = $targetBranchShort
        DOCS_PRD_DIR    = $docsPrdDir
        DOCS_CONTEXT    = $docsContext
        DOCS_ADR_DIR    = $docsAdrDir
        TESTS_BLOCK     = $testsBlock
        TYPECHECK_BLOCK = $typecheckBlock
        COMMIT_STYLE    = $commitStyle
    }
} else {
    # ── Plan phase (bare run, -Plan, or -Yes) ─────────────────────────────────
    # By elimination: neither -SmokeTest nor -Issue, so -Plan, -Yes, or bare.
    Step 'Plan phase'

    $planModel    = $cfg.agents.plan.model
    $planMaxTurns = $cfg.agents.plan.max_turns

    $excluded = Get-DeconflictExclusions -BranchPrefix $cfg.branch_prefix
    Write-Host "  In-progress: $(Format-Exclusions $excluded)" -ForegroundColor DarkGray

    $adrDirRel = if ($cfg.ContainsKey('docs') -and $cfg.docs -is [hashtable] -and $cfg.docs.ContainsKey('adr_dir')) {
        $cfg.docs.adr_dir
    } else { '' }
    $adrDir   = if ($adrDirRel) { Join-Path $RepoRoot $adrDirRel } else { '' }
    $adrNames = if ($adrDir -and (Test-Path $adrDir)) {
        (Get-ChildItem $adrDir -Filter '*.md' | Select-Object -ExpandProperty Name) -join ', '
    } else { '' }

    $labelFlag = if ($cfg.tracker -is [hashtable] -and $cfg.tracker['filter_label']) {
        "--label '$($cfg.tracker.filter_label)'"
    } else { '' }

    $planSubs = @{
        REPO               = $cfg.tracker.repo
        BRANCH_PREFIX      = $cfg.branch_prefix
        IN_PROGRESS_LIST   = Format-Exclusions $excluded
        ADR_FILENAMES      = $adrNames
        TRACKER_LABEL_FLAG = $labelFlag
    }

    $planFile = "$HarnessRoot/prompts/plan.md"
    if (-not (Test-Path $planFile)) { Fail "Plan prompt not found: $planFile" }

    $rendered    = Invoke-RenderPrompt -Template (Get-Content $planFile -Raw) -Substitutions $planSubs
    $promptMount = Get-PromptHostPath -MountPath $mountPath
    Set-Content -Path $promptMount -Value $rendered -Encoding UTF8

    $logFile    = "$HarnessRoot/logs/plan-$(Get-Date -Format 'yyyyMMdd-HHmmss').log"
    $rawLogFile = [System.IO.Path]::ChangeExtension($logFile, 'raw.jsonl')
    $planLogDir = Split-Path $logFile -Parent
    if (-not (Test-Path $planLogDir)) { New-Item -ItemType Directory $planLogDir | Out-Null }

    Write-RunHeader -IssueLabel '?' -Model $planModel -Branch '(pending)' -LogFile $logFile
    Write-Host "  max_turns=$planMaxTurns" -ForegroundColor DarkGray
    Write-LogHeader -Phase 'plan' -LogFile $logFile -RawLogFile $rawLogFile

    $hbState    = @{ turns = 0; elapsed_s = 0; last_action = '' }
    Start-HbTimer
    $accContent = [System.Text.StringBuilder]::new()

    $claudeCmd = "claude --output-format stream-json --verbose --permission-mode bypassPermissions --model $planModel --max-turns $planMaxTurns -p `"`$(cat /workspace/.harness/.current-prompt.md)`""
    $dockerPlan = @(
        'run', '--rm',
        '--volume', "${mountPath}:/workspace",
        '--env',    'CLAUDE_CODE_OAUTH_TOKEN',
        '--env',    'GH_TOKEN',
        # Isolate uv's project venv inside the container so a Windows host's
        # .venv (with .exe shims) doesn't get bind-mounted into a Linux
        # container and break `uv run`. The /tmp path is container-local;
        # each docker run rebuilds it via `uv sync` on demand (~30s).
        '--env',    'UV_PROJECT_ENVIRONMENT=/tmp/venv',
        # Make the host's Postgres + ElasticMQ reachable from inside the
        # container so the agent can run `pytest -m integration`. On Docker
        # Desktop (Win/Mac) host.docker.internal resolves automatically;
        # --add-host makes the same name work on Linux Docker too.
        '--add-host', 'host.docker.internal:host-gateway',
        '--env',    'DATABASE_URL=postgresql+asyncpg://app:app@host.docker.internal:5432/app',
        '--env',    'ALEMBIC_DATABASE_URL=postgresql+psycopg://app:app@host.docker.internal:5432/app',
        '--env',    'QUEUE_URL=http://host.docker.internal:9324/queue/task-queue',
        '--env',    'QUEUE_DLQ_URL=http://host.docker.internal:9324/queue/task-dlq',
        '--workdir', '/workspace',
        $imageName,
        'bash', '-lc', $claudeCmd
    )

    $planExit = -1
    try {
        & docker @dockerPlan 2>&1 | ForEach-Object {
            Add-Content -Path $rawLogFile -Value $_
            try {
                $ev = $_ | ConvertFrom-Json -AsHashtable
                $hbState = Invoke-HeartbeatReduce -State $hbState -Event $ev
                $formatted = Format-StreamEvent -Event $ev
                if ($formatted) {
                    Add-Content -Path $logFile -Value $formatted
                    Add-Content -Path $logFile -Value ''
                }
                if ($ev.type -eq 'assistant' -and $ev.ContainsKey('message') -and $ev.message -is [hashtable] -and $ev.message.ContainsKey('content')) {
                    foreach ($item in @($ev.message.content)) {
                        if ($item -is [hashtable] -and $item.type -eq 'text' -and $item.ContainsKey('text')) {
                            [void]$accContent.Append([string]$item.text)
                        }
                    }
                }
                if ($ev.type -eq 'result' -and $ev.ContainsKey('result')) { [void]$accContent.Append([string]$ev.result) }
                Write-HbLine -State $hbState
            } catch {
                # Non-JSON line (preamble, error, etc.) — write to human log too.
                Add-Content -Path $logFile -Value $_
            }
        }
        $planExit = $LASTEXITCODE
    } finally {
        Remove-Item -ErrorAction SilentlyContinue $promptMount
    }

    Write-LogFooter -LogFile $logFile -RawLogFile $rawLogFile

    if ($planExit -ne 0) {
        Close-HbLine "  FAILED: docker exit $planExit" -Color 'Red'
        exit $planExit
    }
    Close-HbLine "  Plan agent complete." -Color 'Green'

    $parsed = Invoke-ParsePlan -Content $accContent.ToString()
    # Use ContainsKey/subscript to stay safe under StrictMode Latest: dot-notation
    # on a missing hashtable key throws "The property 'X' cannot be found".
    if ($parsed.ContainsKey('Error')) {
        Write-Host "ERROR: Could not parse plan — $($parsed['Error'])" -ForegroundColor Red
        Write-Host "  Raw log: $logFile" -ForegroundColor DarkGray
        exit 1
    }

    $pd  = $parsed['Plan']
    $top = $pd.top
    Write-Host ''
    Write-Host '┌─ Plan ranking ──────────────────────────────────────────────┐' -ForegroundColor Cyan
    $boxW = 62  # inner width (between │ chars)
    function Pad-BoxLine([string]$s) { if ($s.Length -gt $boxW) { $s = $s.Substring(0,$boxW-3) + '...' }; '│' + $s.PadRight($boxW) + '│' }
    Write-Host (Pad-BoxLine "  TOP  #$($top.id) — $($top.title)") -ForegroundColor Green
    Write-Host (Pad-BoxLine "       Branch : $($top.branch)") -ForegroundColor DarkGray
    Write-Host (Pad-BoxLine "       Reason : $($top.reason)") -ForegroundColor DarkGray
    Write-Host (Pad-BoxLine "       AC     : $($top.ac_count) items") -ForegroundColor DarkGray
    # @() coerces null/single-object/array into a real array so .Count is always valid under strict mode.
    $alts    = @($pd.alternatives)
    $blocked = @($pd.blocked)
    # Subscript-style ($h['key']) returns $null for missing keys instead of throwing
    # under StrictMode Latest. alt/b entries aren't schema-validated by parse-plan.ps1.
    if ($alts.Count -gt 0) {
        Write-Host (Pad-BoxLine "  ── Alternatives ────────────────────────────────────────────") -ForegroundColor DarkGray
        foreach ($alt in $alts) {
            Write-Host (Pad-BoxLine "  #$($alt['id']) $($alt['title']) — $($alt['reason'])") -ForegroundColor DarkGray
        }
    }
    if ($blocked.Count -gt 0) {
        Write-Host (Pad-BoxLine "  ── Blocked ─────────────────────────────────────────────────") -ForegroundColor DarkGray
        foreach ($b in $blocked) {
            Write-Host (Pad-BoxLine "  #$($b['id']) $($b['title']) (blocked by #$($b['blocked_by']))") -ForegroundColor Yellow
        }
    }
    Write-Host '└──────────────────────────────────────────────────────────────┘' -ForegroundColor Cyan
    Write-Host ''

    if ($Plan) { exit 0 }

    $confirmed = $false
    if ($Yes) {
        Write-Host "  Auto-confirming #$($top.id) ($($top.title))..." -ForegroundColor Green
        $confirmed = $true
    } else {
        $ans = Read-Host "Run #$($top.id) — $($top.title)? [Y/n]"
        $confirmed = ($ans -eq '' -or $ans -match '^[Yy]')
    }

    if (-not $confirmed) {
        Write-Host '  Exiting — no branch created.' -ForegroundColor DarkGray
        exit 0
    }

    Write-Host "  Selected #$($top.id) — chaining into implement phase..." -ForegroundColor Green
    & $PSCommandPath -Issue ([int]$top.id)
    exit $LASTEXITCODE
}

# ── Implement phase ────────────────────────────────────────────────────────────
# Skipped when -StartPhase is review or merge (the implement work happened
# in an earlier run; we're picking up the worktree mid-pipeline). The lock,
# worktree, and config are still set up above so review/merge can run.

if ($Issue -and $StartPhase -ne 'implement') {
    Write-Host ''
    Write-Host "  Implement phase skipped (-StartPhase $StartPhase)." -ForegroundColor DarkGray
    $ok         = $true
    $implOk     = $true
    $implStatus = "SKIPPED (-StartPhase $StartPhase)"
    $exitCode   = 0
} else {

if (-not (Test-Path $promptFile)) { Fail "Prompt file not found: $promptFile" }

$rawPrompt      = Get-Content $promptFile -Raw
$renderedPrompt = Invoke-RenderPrompt -Template $rawPrompt -Substitutions $subs

# Write rendered prompt to a temp file mounted into the container
$promptMount = Get-PromptHostPath -MountPath $mountPath
Set-Content -Path $promptMount -Value $renderedPrompt -Encoding UTF8

# ── Run container ──────────────────────────────────────────────────────────────

# before-tests hook: runs on host before implement container starts
if ($Issue -and -not $SmokeTest) {
    Invoke-HarnessHook -HookName 'before-tests.sh' -HooksDir "$HarnessRoot/hooks" `
        -Issue $Issue -Branch $branchName -Phase 'implement'
}

Step "Running $runLabel"
Write-Host "  Log → $logFile"

$logDir = Split-Path $logFile -Parent
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory $logDir | Out-Null }

$rawLogFile = [System.IO.Path]::ChangeExtension($logFile, 'raw.jsonl')
Write-LogHeader -Phase $runLabel -LogFile $logFile -RawLogFile $rawLogFile

# Build the claude invocation. For implement runs, pass --model and --max-turns.
$claudeInvocation = if ($implementModel -and $maxTurns) {
    "claude --output-format stream-json --verbose --permission-mode bypassPermissions --model $implementModel --max-turns $maxTurns -p `"`$(cat /workspace/.harness/.current-prompt.md)`""
} else {
    'claude --output-format stream-json --verbose --permission-mode bypassPermissions -p "$(cat /workspace/.harness/.current-prompt.md)"'
}

# Pass the token by reference (no `=value`) so it doesn't appear in
# the host process listing. Docker reads it from our environment.
$dockerArgs = @(
    'run', '--rm',
    '--volume', "${mountPath}:/workspace",
    '--env',    'CLAUDE_CODE_OAUTH_TOKEN',
    '--env',    'GH_TOKEN',
    # Isolate uv's project venv inside the container so a Windows host's
    # .venv (with .exe shims) doesn't get bind-mounted into a Linux
    # container and break `uv run`. The /tmp path is container-local;
    # each docker run rebuilds it via `uv sync` on demand (~30s).
    '--env',    'UV_PROJECT_ENVIRONMENT=/tmp/venv',
    # Make the host's Postgres + ElasticMQ reachable from inside the
    # container so the agent can run `pytest -m integration`. On Docker
    # Desktop (Win/Mac) host.docker.internal resolves automatically;
    # --add-host makes the same name work on Linux Docker too.
    '--add-host', 'host.docker.internal:host-gateway',
    '--env',    'DATABASE_URL=postgresql+asyncpg://app:app@host.docker.internal:5432/app',
    '--env',    'ALEMBIC_DATABASE_URL=postgresql+psycopg://app:app@host.docker.internal:5432/app',
    '--env',    'QUEUE_URL=http://host.docker.internal:9324/queue/task-queue',
    '--env',    'QUEUE_DLQ_URL=http://host.docker.internal:9324/queue/task-dlq',
    '--workdir', '/workspace',
    $imageName,
    'bash', '-lc', $claudeInvocation
)

try {
    & docker @dockerArgs 2>&1 | ForEach-Object {
        Write-FormattedLine -RawLine $_ -LogFile $logFile -RawLogFile $rawLogFile
    }
    $exitCode = $LASTEXITCODE
} finally {
    Remove-Item -ErrorAction SilentlyContinue $promptMount
}

Write-LogFooter -LogFile $logFile -RawLogFile $rawLogFile

# ── Implement result ───────────────────────────────────────────────────────────

$ok          = $exitCode -eq 0
$implStatus  = if ($ok) { 'COMPLETE' } else { "FAILED (exit $exitCode)" }
$implOk      = $ok

}

# after-implement hook: runs on host after implement container exits (success or fail)
if ($Issue -and -not $SmokeTest) {
    Invoke-HarnessHook -HookName 'after-implement.sh' -HooksDir "$HarnessRoot/hooks" `
        -Issue $Issue -Branch $branchName -Phase 'implement'
}

if (-not $ok -and $SmokeTest) {
    Write-Host ''
    Write-Host "  Smoke test FAILED (exit $exitCode)." -ForegroundColor Red
    Write-Host "  Log saved to: $logFile" -ForegroundColor DarkGray
    exit $exitCode
}

# Rate-limit detection: surface a ready-made resume command
if (-not $ok -and $Issue -and (Test-Path $logFile)) {
    $logContent = Get-Content $logFile -Raw -ErrorAction SilentlyContinue
    if ($logContent -match 'Rate limit exceeded|usage_limit_exceeded') {
        Write-Host ''
        Write-Host '  Rate limit hit. Resume with:' -ForegroundColor Yellow
        Write-Host "  pwsh ./.harness/run.ps1 -Issue $Issue -Resume" -ForegroundColor Yellow
    }
}

# ── Review phase ───────────────────────────────────────────────────────────────
# Runs only after a successful implement run (Issue set, not SmokeTest, not SkipReview).
# Also skipped when -StartPhase merge (review happened in an earlier run).

$reviewOk     = $false
$reviewStatus = '⊝ SKIPPED'

if ($Issue -and -not $SmokeTest -and $StartPhase -eq 'merge') {
    # -StartPhase merge: review was done previously; pretend it succeeded so
    # the merge phase below runs. The summary box surfaces this explicitly.
    $reviewOk     = $true
    $reviewStatus = '⊝ SKIPPED (-StartPhase merge)'
} elseif ($ok -and $Issue -and -not $SmokeTest -and -not $SkipReview) {
    $reviewModel    = $cfg.agents.review.model
    $reviewMaxTurns = $cfg.agents.review.max_turns

    Step 'Review phase'
    Write-Host "  model=$reviewModel  max_turns=$reviewMaxTurns" -ForegroundColor DarkGray

    # $targetBranch was computed once during the implement-subs block above and is reused here.

    $codingStandardsPath = "$HarnessRoot/CODING_STANDARDS.md"
    $codingStandardsBlock = if (Test-Path $codingStandardsPath) {
        Get-Content $codingStandardsPath -Raw
    } else { '' }

    $reviewSubs = @{
        ISSUE                  = "$Issue"
        BRANCH                 = $branchName
        TARGET_BRANCH          = $targetBranch
        DOCS_CONTEXT           = $docsContext
        DOCS_ADR_DIR           = $docsAdrDir
        CODING_STANDARDS_BLOCK = $codingStandardsBlock
    }

    $reviewPromptFile = "$HarnessRoot/prompts/review.md"
    if (-not (Test-Path $reviewPromptFile)) { Fail "Review prompt not found: $reviewPromptFile" }

    $renderedReview  = Invoke-RenderPrompt -Template (Get-Content $reviewPromptFile -Raw) -Substitutions $reviewSubs
    $reviewMount     = Get-PromptHostPath -MountPath $mountPath
    Set-Content -Path $reviewMount -Value $renderedReview -Encoding UTF8

    $reviewLogFile    = "$HarnessRoot/logs/review-$Issue-$(Get-Date -Format 'yyyyMMdd-HHmmss').log"
    $reviewRawLogFile = [System.IO.Path]::ChangeExtension($reviewLogFile, 'raw.jsonl')
    Write-Host "  Log → $reviewLogFile" -ForegroundColor DarkGray
    Write-LogHeader -Phase "review-$Issue" -LogFile $reviewLogFile -RawLogFile $reviewRawLogFile

    $reviewCmd = "claude --output-format stream-json --verbose --permission-mode bypassPermissions --model $reviewModel --max-turns $reviewMaxTurns -p `"`$(cat /workspace/.harness/.current-prompt.md)`""
    $dockerReview = @(
        'run', '--rm',
        '--volume', "${mountPath}:/workspace",
        '--env',    'CLAUDE_CODE_OAUTH_TOKEN',
        '--env',    'GH_TOKEN',
        # Isolate uv's project venv inside the container so a Windows host's
        # .venv (with .exe shims) doesn't get bind-mounted into a Linux
        # container and break `uv run`. The /tmp path is container-local;
        # each docker run rebuilds it via `uv sync` on demand (~30s).
        '--env',    'UV_PROJECT_ENVIRONMENT=/tmp/venv',
        # Make the host's Postgres + ElasticMQ reachable from inside the
        # container so the agent can run `pytest -m integration`. On Docker
        # Desktop (Win/Mac) host.docker.internal resolves automatically;
        # --add-host makes the same name work on Linux Docker too.
        '--add-host', 'host.docker.internal:host-gateway',
        '--env',    'DATABASE_URL=postgresql+asyncpg://app:app@host.docker.internal:5432/app',
        '--env',    'ALEMBIC_DATABASE_URL=postgresql+psycopg://app:app@host.docker.internal:5432/app',
        '--env',    'QUEUE_URL=http://host.docker.internal:9324/queue/task-queue',
        '--env',    'QUEUE_DLQ_URL=http://host.docker.internal:9324/queue/task-dlq',
        '--workdir', '/workspace',
        $imageName,
        'bash', '-lc', $reviewCmd
    )

    $reviewExit = -1
    try {
        & docker @dockerReview 2>&1 | ForEach-Object {
            Write-FormattedLine -RawLine $_ -LogFile $reviewLogFile -RawLogFile $reviewRawLogFile
        }
        $reviewExit = $LASTEXITCODE
    } finally {
        Remove-Item -ErrorAction SilentlyContinue $reviewMount
    }

    Write-LogFooter -LogFile $reviewLogFile -RawLogFile $reviewRawLogFile

    $reviewOk = $reviewExit -eq 0
    if ($reviewOk) {
        $reviewStatus = '✓ COMPLETE'
    } else {
        $reviewStatus = "✗ FAILED (exit $reviewExit)"
        if (Test-Path $reviewLogFile) {
            $reviewLog = Get-Content $reviewLogFile -Raw -ErrorAction SilentlyContinue
            if ($reviewLog -match 'Rate limit exceeded|usage_limit_exceeded') {
                Write-Host '  Rate limit hit during review. Re-run with -SkipReview to skip, or retry.' -ForegroundColor Yellow
            }
        }
    }
}

# ── Merge phase ────────────────────────────────────────────────────────────────
# Runs only after a successful review (Issue set, not SmokeTest, not SkipReview, not SkipMerge).

$mergeOk     = $false
$mergeStatus = '⊝ SKIPPED'
$prUrl       = ''

if ($reviewOk -and $Issue -and -not $SmokeTest -and -not $SkipMerge) {
    $mergeModel    = $cfg.agents.merge.model
    $mergeMaxTurns = $cfg.agents.merge.max_turns

    Step 'Merge phase'
    Write-Host "  model=$mergeModel  max_turns=$mergeMaxTurns" -ForegroundColor DarkGray

    $testsBlock = Get-ConfigBlock -Config $cfg -Section 'tests' -WorkDir $RepoRoot

    $mergeSubs = @{
        ISSUE         = "$Issue"
        BRANCH        = $branchName
        REPO          = $cfg.tracker.repo
        TARGET_BRANCH = $targetBranch
        TESTS_BLOCK   = $testsBlock
    }

    $mergePromptFile = "$HarnessRoot/prompts/merge.md"
    if (-not (Test-Path $mergePromptFile)) { Fail "Merge prompt not found: $mergePromptFile" }

    $renderedMerge = Invoke-RenderPrompt -Template (Get-Content $mergePromptFile -Raw) -Substitutions $mergeSubs
    $mergeMount    = Get-PromptHostPath -MountPath $mountPath
    Set-Content -Path $mergeMount -Value $renderedMerge -Encoding UTF8

    $mergeLogFile    = "$HarnessRoot/logs/merge-$Issue-$(Get-Date -Format 'yyyyMMdd-HHmmss').log"
    $mergeRawLogFile = [System.IO.Path]::ChangeExtension($mergeLogFile, 'raw.jsonl')
    Write-Host "  Log → $mergeLogFile" -ForegroundColor DarkGray
    Write-LogHeader -Phase "merge-$Issue" -LogFile $mergeLogFile -RawLogFile $mergeRawLogFile

    $mergeCmd = "claude --output-format stream-json --verbose --permission-mode bypassPermissions --model $mergeModel --max-turns $mergeMaxTurns -p `"`$(cat /workspace/.harness/.current-prompt.md)`""
    $dockerMerge = @(
        'run', '--rm',
        '--volume', "${mountPath}:/workspace",
        '--env',    'CLAUDE_CODE_OAUTH_TOKEN',
        '--env',    'GH_TOKEN',
        # Isolate uv's project venv inside the container so a Windows host's
        # .venv (with .exe shims) doesn't get bind-mounted into a Linux
        # container and break `uv run`. The /tmp path is container-local;
        # each docker run rebuilds it via `uv sync` on demand (~30s).
        '--env',    'UV_PROJECT_ENVIRONMENT=/tmp/venv',
        # Make the host's Postgres + ElasticMQ reachable from inside the
        # container so the agent can run `pytest -m integration`. On Docker
        # Desktop (Win/Mac) host.docker.internal resolves automatically;
        # --add-host makes the same name work on Linux Docker too.
        '--add-host', 'host.docker.internal:host-gateway',
        '--env',    'DATABASE_URL=postgresql+asyncpg://app:app@host.docker.internal:5432/app',
        '--env',    'ALEMBIC_DATABASE_URL=postgresql+psycopg://app:app@host.docker.internal:5432/app',
        '--env',    'QUEUE_URL=http://host.docker.internal:9324/queue/task-queue',
        '--env',    'QUEUE_DLQ_URL=http://host.docker.internal:9324/queue/task-dlq',
        '--workdir', '/workspace',
        $imageName,
        'bash', '-lc', $mergeCmd
    )

    $mergeAccContent = [System.Text.StringBuilder]::new()
    $mergeExit = -1
    try {
        & docker @dockerMerge 2>&1 | ForEach-Object {
            Add-Content -Path $mergeRawLogFile -Value $_
            try {
                $ev = $_ | ConvertFrom-Json -AsHashtable
                $formatted = Format-StreamEvent -Event $ev
                if ($formatted) {
                    Write-Host $formatted
                    Add-Content -Path $mergeLogFile -Value $formatted
                    Add-Content -Path $mergeLogFile -Value ''
                }
                if ($ev.type -eq 'assistant' -and $ev.ContainsKey('message') -and $ev.message -is [hashtable] -and $ev.message.ContainsKey('content')) {
                    foreach ($item in @($ev.message.content)) {
                        if ($item -is [hashtable] -and $item.type -eq 'text' -and $item.ContainsKey('text')) {
                            [void]$mergeAccContent.Append([string]$item.text)
                        }
                    }
                }
                if ($ev.type -eq 'result' -and $ev.ContainsKey('result')) { [void]$mergeAccContent.Append([string]$ev.result) }
            } catch {
                Write-Host $_
                Add-Content -Path $mergeLogFile -Value $_
            }
        }
        $mergeExit = $LASTEXITCODE
    } finally {
        Remove-Item -ErrorAction SilentlyContinue $mergeMount
    }

    Write-LogFooter -LogFile $mergeLogFile -RawLogFile $mergeRawLogFile

    # Claude exit 0 means the model run terminated cleanly — NOT that the
    # mission succeeded. A merge agent that politely stops on a HARD RULE
    # ("can't open PR because the branch has an orphan root") still exits
    # 0, but no PR was created. The harness then misreports ✓ COMPLETE.
    # Mission verification: claude must (a) exit 0 AND (b) leave a PR URL
    # discoverable either in its text output or via `gh pr list`.
    $mergeOk = $false
    if ($mergeExit -ne 0) {
        $mergeStatus = "✗ FAILED (exit $mergeExit)"
    } else {
        # Bounded to owner/repo slug chars so trailing markdown punctuation
        # (backticks, parens) is not captured.
        if ($mergeAccContent.ToString() -match 'https://github\.com/[\w.-]+/[\w.-]+/pull/\d+') {
            $prUrl = $Matches[0]
        }
        # Fallback: if no URL surfaced in the agent's text, ask GitHub
        # directly. Cheap (single API call) and tolerant: if `gh` isn't
        # configured or the branch isn't pushed, this just returns empty.
        if (-not $prUrl) {
            $ghOut = & gh pr list --repo $cfg.tracker.repo --head $branchName --json url --jq '.[0].url' 2>$null
            if ($LASTEXITCODE -eq 0 -and $ghOut) { $prUrl = "$ghOut".Trim() }
        }
        if ($prUrl) {
            $mergeOk = $true
            $mergeStatus = '✓ COMPLETE'
        } else {
            $mergeStatus = '✗ FAILED (no PR opened — claude exited cleanly but mission incomplete)'
        }
    }
} elseif ($Issue -and -not $SmokeTest -and $SkipMerge) {
    Write-Host ''
    Write-Host '  Merge phase skipped (-SkipMerge).' -ForegroundColor DarkGray
}

# ── Issue lifecycle cleanup ───────────────────────────────────────────────────
# Always release the lock when an Issue-bound run finishes (otherwise the
# next invocation would see a stale lock and warn). Remove the worktree only
# when every phase that ran finished cleanly — otherwise keep it so the user
# can -Resume.

if ($Issue -and -not $SmokeTest) {
    Invoke-ReleaseIssueLock -RepoRoot $RepoRoot -IssueNumber $Issue | Out-Null
    # Clean exit — trap no longer needs to release this lock.
    $script:HeldLockIssue = 0

    $fullSuccess = $implOk `
        -and ($reviewStatus -like '✓*') `
        -and ($mergeStatus  -like '✓*')

    if ($fullSuccess) {
        try {
            $null = Remove-IssueWorktree -RepoRoot $RepoRoot -IssueNumber $Issue
            Write-Host "  Worktree cleaned up (slice complete)." -ForegroundColor DarkGray
        } catch {
            Write-Warning "  Worktree cleanup failed: $_"
            Write-Warning "  Manual cleanup: pwsh ./.harness/run.ps1 -Cleanup $Issue -Force"
        }
    } else {
        Write-Host "  Worktree retained at: $worktreePath" -ForegroundColor DarkGray
        Write-Host "  Resume with: pwsh ./.harness/run.ps1 -Issue $Issue -Resume" -ForegroundColor DarkGray
    }
}

# ── Final summary box ──────────────────────────────────────────────────────────

$anyFailed  = (-not $implOk) -or ($reviewStatus -like '✗*') -or ($mergeStatus -like '✗*')
$finalColor = if ($anyFailed) { 'Red' } else { 'Green' }

$implStatusLine   = if ($implStatus -match 'SKIPPED') { "⊝ $implStatus" }
                    elseif ($implOk)                  { '✓ COMPLETE' }
                    else                              { "✗ $implStatus" }
$reviewStatusLine = $reviewStatus
$mergeStatusLine  = $mergeStatus

Write-Host ''
Write-Host ('╔' + '═' * 58 + '╗') -ForegroundColor $finalColor
if ($Issue) {
    Write-Host ("║  Pipeline result — issue #$Issue".PadRight(59) + '║') -ForegroundColor $finalColor
} else {
    Write-Host ("║  Pipeline result".PadRight(59) + '║') -ForegroundColor $finalColor
}
Write-Host ('╠' + '═' * 58 + '╣') -ForegroundColor $finalColor
$phaseLabel = if ($SmokeTest) { 'smoke-test' } else { 'implement ' }
Write-Host ("║  $phaseLabel : $implStatusLine".PadRight(59) + '║') -ForegroundColor $finalColor
if ($Issue -and -not $SmokeTest) {
    Write-Host ("║  review    : $reviewStatusLine".PadRight(59) + '║') -ForegroundColor $finalColor
    Write-Host ("║  merge     : $mergeStatusLine".PadRight(59) + '║') -ForegroundColor $finalColor
}
Write-Host ('╠' + '═' * 58 + '╣') -ForegroundColor $finalColor
if ($branchName) {
    Write-Host ("║  branch    : $branchName".PadRight(59) + '║') -ForegroundColor $finalColor
}
Write-Host ("║  log       : $logFile".PadRight(59) + '║') -ForegroundColor $finalColor
if ($prUrl) {
    Write-Host ("║  PR        : $prUrl".PadRight(59) + '║') -ForegroundColor $finalColor
}
Write-Host ('╠' + '═' * 58 + '╣') -ForegroundColor $finalColor
if ($anyFailed -and $Issue) {
    Write-Host ("║  resume    : pwsh ./.harness/run.ps1 -Issue $Issue -Resume".PadRight(59) + '║') -ForegroundColor Yellow
} elseif ($mergeOk -and $prUrl) {
    Write-Host ('║  next      : merge the PR on GitHub to close the issue'.PadRight(59) + '║') -ForegroundColor $finalColor
} elseif ($implOk -and -not $Issue) {
    Write-Host ('║  next      : run with -Issue N to implement a specific issue'.PadRight(59) + '║') -ForegroundColor $finalColor
}
Write-Host ('╚' + '═' * 58 + '╝') -ForegroundColor $finalColor

exit $exitCode
