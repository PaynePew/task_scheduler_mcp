#Requires -Version 7
# Pure formatter: stream-json event → human-readable line(s).
# Returns $null for events we deliberately skip (silent in human view).

function Format-StreamEvent {
    param([Parameter(Mandatory)][hashtable]$Event)

    $lines = switch ($Event.type) {
        'system'           { Format-SystemInit $Event }
        'rate_limit_event' { Format-RateLimit $Event }
        'assistant'        { Format-Assistant $Event }
        'result'           { Format-Result $Event }
        default            { $null }
    }

    if (-not $lines) { return $null }
    return ($lines -join "`n")
}

function Format-SystemInit {
    param([hashtable]$Event)
    if ($Event.subtype -ne 'init') { return $null }

    $model = $Event.model
    $cwd   = $Event.cwd
    $ver   = if ($Event.ContainsKey('claude_code_version')) { $Event.claude_code_version } else { '?' }
    $perm  = if ($Event.ContainsKey('permissionMode'))      { $Event.permissionMode      } else { '?' }

    return @(
        "[INIT]    model=$model  cwd=$cwd"
        "          version=$ver  permissionMode=$perm"
    )
}

function Format-RateLimit {
    param([hashtable]$Event)
    if (-not $Event.ContainsKey('rate_limit_info')) { return $null }
    $info = $Event.rate_limit_info
    if (-not $info -or $info -isnot [hashtable]) { return $null }

    # New Claude format (2025+): { status, resetsAt, rateLimitType, overageStatus, isUsingOverage, ... }
    # Legacy format had: { utilization, surpassedThreshold, status, rateLimitType, resetsAt }
    # Handle both — missing keys default to neutral values rather than throwing under strict mode.
    $window     = if ($info.ContainsKey('rateLimitType')) { ($info.rateLimitType -replace '_', '-') } else { 'unknown' }
    $resetUtc   = if ($info.ContainsKey('resetsAt')) {
        try { [DateTimeOffset]::FromUnixTimeSeconds([int64]$info.resetsAt).UtcDateTime.ToString('yyyy-MM-dd HH:mm') }
        catch { 'unknown' }
    } else { 'unknown' }
    $status     = if ($info.ContainsKey('status')) { [string]$info.status } else { '' }

    $marker = if ($status -eq 'allowed_warning' -or $status -eq 'exceeded') { '[⚠ RATE]' } else { '[RATE]  ' }

    if ($info.ContainsKey('utilization') -and $info.ContainsKey('surpassedThreshold')) {
        # Legacy format with usage percentage.
        $util       = [int]([double]$info.utilization * 100)
        $threshold  = [int]([double]$info.surpassedThreshold * 100)
        return @(
            "$marker  $window window: $util% used (>$threshold% warning)"
            "          resets $resetUtc UTC"
        )
    }

    # New format — no percentage; surface status + reset time.
    return @(
        "$marker  $window window: status=$status"
        "          resets $resetUtc UTC"
    )
}

function Format-Assistant {
    param([hashtable]$Event)
    if (-not $Event.ContainsKey('message')) { return $null }
    if (-not $Event.message.ContainsKey('content')) { return $null }

    $lines  = @()
    $tokens = if ($Event.message.ContainsKey('usage') -and $Event.message.usage.ContainsKey('output_tokens')) {
        $Event.message.usage.output_tokens
    } else { $null }

    foreach ($item in @($Event.message.content)) {
        if ($item -isnot [hashtable]) { continue }
        switch ($item.type) {
            'text' {
                $text = ($item.text -replace '\s+', ' ').Trim()
                $lines += "[ASSIST]  $text"
                if ($null -ne $tokens) {
                    $lines += "          (text · $tokens output tokens)"
                }
            }
            'tool_use' {
                $name = if ($item.ContainsKey('name')) { $item.name } else { 'tool' }
                $lines += "[ASSIST]  tool:$name"
            }
        }
    }

    if ($lines.Count -eq 0) { return $null }
    return $lines
}

function Format-Result {
    param([hashtable]$Event)

    $isError  = if ($Event.ContainsKey('is_error')) { [bool]$Event.is_error } else { $false }
    $marker   = if ($isError) { '✗ FAIL' } else { '✓ success' }
    $turns    = if ($Event.ContainsKey('num_turns'))    { $Event.num_turns }    else { 0 }
    $turnUnit = if ($turns -eq 1) { 'turn' } else { 'turns' }
    $durMs    = if ($Event.ContainsKey('duration_ms'))   { [double]$Event.duration_ms } else { 0 }
    $durSec   = ($durMs / 1000).ToString('0.0')
    $cost     = if ($Event.ContainsKey('total_cost_usd')) { [double]$Event.total_cost_usd } else { 0 }
    $costStr  = '$' + $cost.ToString('0.0000')

    $lines = @("[RESULT]  $marker  ·  $turns $turnUnit  ·  ${durSec}s wall  ·  $costStr")

    if ($Event.ContainsKey('usage')) {
        $u    = $Event.usage
        $inp  = if ($u.ContainsKey('input_tokens'))            { $u.input_tokens }            else { 0 }
        $outp = if ($u.ContainsKey('output_tokens'))           { $u.output_tokens }           else { 0 }
        $cch  = if ($u.ContainsKey('cache_read_input_tokens')) { $u.cache_read_input_tokens } else { 0 }
        $lines += "          tokens: $inp in / $outp out / $cch cached"
    }

    return $lines
}
