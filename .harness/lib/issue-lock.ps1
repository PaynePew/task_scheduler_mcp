#Requires -Version 7
<#
Per-issue lock files for parallel-run coordination.

Each in-flight slice acquires .harness/locks/issue-<N>.lock containing the
holder's PID, branch, phase, and timestamp. A second `run.ps1` invocation
for the same issue refuses unless:
  - The holder PID is dead (stale lock, auto-takeover with warning), OR
  - The caller passes -Force (explicit override, warns user).

Locks are advisory: they coordinate the harness, not the filesystem. If a
user manually edits files in someone else's worktree, no lock will save
them. The contract is "no two run.ps1 processes touch the same issue".

Lock file format (JSON):
{
  "pid": 12345,
  "branch": "issue-4-foo",
  "phase": "implement",
  "acquired_at": "2026-05-12T15:30:00.0000000+08:00",
  "machine": "MAXL-LAPTOP"
}
#>

function Get-IssueLockPath {
    param(
        [Parameter(Mandatory)][string]$RepoRoot,
        [Parameter(Mandatory)][int]$IssueNumber
    )
    return Join-Path $RepoRoot ".harness/locks/issue-$IssueNumber.lock"
}

function Read-IssueLock {
    <#
    Returns the lock contents as a hashtable, or $null if no lock file
    exists OR the file is unreadable/corrupt. Never throws.
    #>
    param(
        [Parameter(Mandatory)][string]$RepoRoot,
        [Parameter(Mandatory)][int]$IssueNumber
    )
    $path = Get-IssueLockPath -RepoRoot $RepoRoot -IssueNumber $IssueNumber
    if (-not (Test-Path $path)) { return $null }
    try {
        return Get-Content $path -Raw -ErrorAction Stop | ConvertFrom-Json -AsHashtable -ErrorAction Stop
    } catch {
        return $null
    }
}

function Test-PidAlive {
    <#
    Returns $true if a process with the given PID is currently running.
    Returns $false on missing or invalid PIDs. Never throws.
    #>
    param(
        [Parameter(Mandatory)][int]$PidValue,
        [scriptblock]$GetProcess = { param($p) Get-Process -Id $p -ErrorAction Stop }
    )
    if ($PidValue -le 0) { return $false }
    try {
        $null = & $GetProcess $PidValue
        return $true
    } catch {
        return $false
    }
}

function Invoke-AcquireIssueLock {
    <#
    Acquire the lock for an issue. Returns the lock file path on success.

    Behavior on contested lock:
    - If holder PID is alive AND -Force not set: throws with diagnostic.
    - If holder PID is alive AND -Force is set: takes over with warning.
    - If holder PID is dead (stale): takes over with warning.
    - If no existing lock: creates fresh.
    #>
    param(
        [Parameter(Mandatory)][string]$RepoRoot,
        [Parameter(Mandatory)][int]$IssueNumber,
        [Parameter(Mandatory)][string]$BranchName,
        [Parameter(Mandatory)][string]$Phase,
        [int]$CurrentPid = $PID,
        [string]$Machine = $env:COMPUTERNAME,
        [switch]$Force,
        [scriptblock]$IsPidAlive = { param($p) Test-PidAlive -PidValue $p }
    )

    $lockPath = Get-IssueLockPath -RepoRoot $RepoRoot -IssueNumber $IssueNumber
    $lockDir = Split-Path $lockPath -Parent
    if (-not (Test-Path $lockDir)) {
        New-Item -ItemType Directory -Path $lockDir -Force | Out-Null
    }

    $existing = Read-IssueLock -RepoRoot $RepoRoot -IssueNumber $IssueNumber
    if ($existing) {
        $heldByPid = if ($existing.ContainsKey('pid')) { [int]$existing.pid } else { 0 }
        $alive = & $IsPidAlive $heldByPid
        if ($alive) {
            if (-not $Force) {
                $heldBranch = if ($existing.ContainsKey('branch')) { $existing.branch } else { '<unknown>' }
                $heldPhase  = if ($existing.ContainsKey('phase'))  { $existing.phase }  else { '<unknown>' }
                $heldSince  = if ($existing.ContainsKey('acquired_at')) { $existing.acquired_at } else { '<unknown>' }
                throw "Issue #$IssueNumber is locked by PID $heldByPid (branch: $heldBranch, phase: $heldPhase, since: $heldSince). Re-run with -Force to override."
            }
            Write-Warning "Forcing lock takeover for issue #$IssueNumber from live PID $heldByPid"
        } else {
            Write-Warning "Stale lock for issue #$IssueNumber (PID $heldByPid is dead) — taking over"
        }
    }

    $lock = [ordered]@{
        pid          = $CurrentPid
        branch       = $BranchName
        phase        = $Phase
        acquired_at  = (Get-Date -Format 'o')
        machine      = $Machine
    }
    Set-Content -Path $lockPath -Value ($lock | ConvertTo-Json) -Encoding UTF8 -NoNewline
    return $lockPath
}

function Invoke-ReleaseIssueLock {
    <#
    Release the lock for an issue. Idempotent — silently no-ops if the
    lock file doesn't exist. Returns $true if a lock was removed.
    #>
    param(
        [Parameter(Mandatory)][string]$RepoRoot,
        [Parameter(Mandatory)][int]$IssueNumber
    )
    $path = Get-IssueLockPath -RepoRoot $RepoRoot -IssueNumber $IssueNumber
    if (-not (Test-Path $path)) { return $false }
    Remove-Item $path -Force -ErrorAction SilentlyContinue
    return $true
}

function Get-IssueLockList {
    <#
    Returns the list of issue numbers with active lock files on disk.
    Does not validate liveness — caller can pair with Test-PidAlive to
    distinguish active vs stale.
    #>
    param(
        [Parameter(Mandatory)][string]$RepoRoot
    )
    $lockDir = Join-Path $RepoRoot '.harness/locks'
    if (-not (Test-Path $lockDir)) { return @() }

    $issues = @()
    Get-ChildItem -Path $lockDir -Filter 'issue-*.lock' -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match '^issue-(\d+)\.lock$' } |
        ForEach-Object { $issues += [int]$Matches[1] }
    return $issues
}
