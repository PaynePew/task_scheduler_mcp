#Requires -Version 7
# Extracts and validates the <plan>...</plan> JSON block from claude stdout.

function Invoke-ParsePlan {
    param(
        [Parameter(Mandatory)][string]$Content
    )

    # Find all <plan>...</plan> blocks; use the last one if multiple appear.
    # Use $planMatches (not $matches) so we don't shadow the $Matches auto-variable.
    $planMatches = [regex]::Matches($Content, '(?s)<plan>(.*?)</plan>')

    if ($planMatches.Count -eq 0) {
        return @{ Error = 'No <plan> block found in content.' }
    }

    $jsonText = $planMatches[$planMatches.Count - 1].Groups[1].Value.Trim()

    $plan = $null
    try {
        $plan = $jsonText | ConvertFrom-Json -AsHashtable -ErrorAction Stop
    } catch {
        return @{ Error = "Malformed JSON in <plan> block: $_" }
    }

    foreach ($key in @('top', 'alternatives', 'blocked')) {
        if (-not $plan.ContainsKey($key)) {
            return @{ Error = "Missing required key '$key' in plan JSON." }
        }
    }

    # Normalize optional collections so downstream `.Count` access is safe.
    # JSON `null` (or any non-array value) becomes @() — the caller treats
    # both "absent" and "empty" the same way.
    foreach ($key in @('alternatives', 'blocked')) {
        if ($null -eq $plan[$key]) {
            $plan[$key] = @()
        } elseif ($plan[$key] -isnot [array] -and $plan[$key] -isnot [System.Collections.IList]) {
            return @{ Error = "'$key' must be an array (or null) in plan JSON, got: $($plan[$key].GetType().Name)" }
        }
    }

    $top = $plan.top
    if ($top -isnot [hashtable]) {
        return @{ Error = "'top' must be an object in plan JSON." }
    }
    foreach ($field in @('id', 'title', 'branch', 'reason', 'ac_count')) {
        if (-not $top.ContainsKey($field)) {
            return @{ Error = "Missing required field 'top.$field' in plan JSON." }
        }
    }
    if ([int]$top.id -le 0) {
        return @{ Error = "'top.id' must be a positive integer in plan JSON." }
    }

    return @{ Plan = $plan }
}
