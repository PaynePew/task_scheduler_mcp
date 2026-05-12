#Requires -Version 7
# Returns the set of issue numbers already claimed by in-progress branches or open PRs.
# Parameters LocalBranches and GhPrListJson are injectable for testing.

function Get-DeconflictExclusions {
    param(
        [Parameter(Mandatory)][string]$BranchPrefix,
        [string[]]$LocalBranches = $null,  # null = read from git branch
        [string]$GhPrListJson    = $null   # null = run gh pr list; empty string = no PRs
    )

    $excluded = [System.Collections.Generic.HashSet[int]]::new()
    # Branch naming: {branch_prefix}{N}-{description}, e.g. kanban-issue42-my-feature
    $pattern  = "^$([regex]::Escape($BranchPrefix))(\d+)-"

    # ── Local branches ────────────────────────────────────────────────────────
    if ($null -eq $LocalBranches) {
        $LocalBranches = (& git branch -a --format='%(refname:short)' 2>&1)
    }

    foreach ($branch in $LocalBranches) {
        # Strip remote-tracking prefix (e.g. "origin/kanban-issue42-foo" → "kanban-issue42-foo")
        $localName = $branch -replace '^[^/]+/', ''
        if ($localName -match $pattern) {
            [void]$excluded.Add([int]$Matches[1])
        }
        # Malformed / non-matching names silently skipped
    }

    # ── Open PRs ──────────────────────────────────────────────────────────────
    if ($null -eq $GhPrListJson) {
        try {
            $GhPrListJson = & gh pr list --state open --json number,headRefName 2>&1
            if ($LASTEXITCODE -ne 0) { $GhPrListJson = $null }
        } catch {
            $GhPrListJson = $null
        }
    }

    if ($GhPrListJson) {
        try {
            $prs = @($GhPrListJson | ConvertFrom-Json)  # @() forces array even for single PR
            foreach ($pr in $prs) {
                # Use PSObject property bag lookup so a missing field returns $null
                # instead of throwing under StrictMode Latest.
                $headRef = $pr.PSObject.Properties['headRefName']
                if ($headRef -and $headRef.Value -match $pattern) {
                    [void]$excluded.Add([int]$Matches[1])
                }
            }
        } catch {
            # Malformed PR JSON — skip PR deconflict, continue with local results
        }
    }

    return $excluded
}
