function Import-HarnessConfig {
    param(
        [Parameter(Mandatory)][string]$ConfigPath
    )

    if (-not (Test-Path $ConfigPath)) {
        throw "Config file not found: $ConfigPath"
    }

    $config = @{}
    $currentL1 = $null
    $currentL2 = $null

    foreach ($line in (Get-Content $ConfigPath)) {
        if ($line -match '^\s*#' -or $line -match '^\s*$') { continue }
        if ($line -match "`t") {
            throw "Tab indentation found in $ConfigPath (line: '$($line.TrimEnd())'). Use spaces only."
        }

        # L3: 4-space indent with value  (e.g. "    model: foo")
        if ($currentL1 -and $currentL2 -and $line -match '^    ([A-Za-z][A-Za-z0-9_-]*): *(.+)$') {
            $config[$currentL1][$currentL2][$Matches[1]] = $Matches[2].Trim()
        }
        # L2: 2-space indent with value  (e.g. "  type: github")
        elseif ($currentL1 -and $line -match '^  ([A-Za-z][A-Za-z0-9_-]*): *(.+)$') {
            if ($config[$currentL1] -isnot [hashtable]) { $config[$currentL1] = @{} }
            $config[$currentL1][$Matches[1]] = $Matches[2].Trim()
            $currentL2 = $null
        }
        # L2: 2-space indent without value — opens L3 block  (e.g. "  implement:")
        elseif ($currentL1 -and $line -match '^  ([A-Za-z][A-Za-z0-9_-]*): *$') {
            if ($config[$currentL1] -isnot [hashtable]) { $config[$currentL1] = @{} }
            $config[$currentL1][$Matches[1]] = @{}
            $currentL2 = $Matches[1]
        }
        # L1: top-level key (with or without value)
        elseif ($line -match '^([A-Za-z][A-Za-z0-9_-]*): *(.*)$') {
            $key   = $Matches[1]
            $value = $Matches[2].Trim()
            if ($value -eq '') {
                $config[$key] = @{}
                $currentL1 = $key
                $currentL2 = $null
            } else {
                $config[$key] = $value
                $currentL1 = $null
                $currentL2 = $null
            }
        }
    }

    foreach ($key in @('image', 'branch_prefix')) {
        if ([string]::IsNullOrWhiteSpace($config[$key])) {
            throw "Missing required config key '$key' in $ConfigPath."
        }
    }

    if (-not ($config['tracker'] -is [hashtable]) -or -not $config['tracker']['type']) {
        throw "Missing required config key 'tracker.type' in $ConfigPath."
    }
    if ($config['tracker']['type'] -ne 'github') {
        throw "tracker.type must be 'github' (v1 only supports github). Got: '$($config['tracker']['type'])' in $ConfigPath."
    }
    if ([string]::IsNullOrWhiteSpace($config['tracker']['repo'])) {
        throw "Missing required config key 'tracker.repo' in $ConfigPath."
    }

    if ($config['defaults'] -isnot [hashtable]) { $config['defaults'] = @{} }
    if (-not $config['defaults']['model']) { $config['defaults']['model'] = 'claude-sonnet-4-6' }

    if ($config['agents'] -isnot [hashtable]) { $config['agents'] = @{} }
    if ($config['agents']['implement'] -isnot [hashtable]) { $config['agents']['implement'] = @{} }
    if (-not $config['agents']['implement']['model'])     { $config['agents']['implement']['model']     = 'claude-sonnet-4-6' }
    if (-not $config['agents']['implement']['max_turns']) { $config['agents']['implement']['max_turns'] = '80' }
    if ($config['agents']['plan'] -isnot [hashtable]) { $config['agents']['plan'] = @{} }
    if (-not $config['agents']['plan']['model'])     { $config['agents']['plan']['model']     = 'claude-opus-4-7' }
    if (-not $config['agents']['plan']['max_turns']) { $config['agents']['plan']['max_turns'] = '10' }
    if ($config['agents']['review'] -isnot [hashtable]) { $config['agents']['review'] = @{} }
    if (-not $config['agents']['review']['model'])     { $config['agents']['review']['model']     = 'claude-opus-4-7' }
    if (-not $config['agents']['review']['max_turns']) { $config['agents']['review']['max_turns'] = '30' }
    if ($config['agents']['merge'] -isnot [hashtable]) { $config['agents']['merge'] = @{} }
    if (-not $config['agents']['merge']['model'])     { $config['agents']['merge']['model']     = 'claude-sonnet-4-6' }
    if (-not $config['agents']['merge']['max_turns']) { $config['agents']['merge']['max_turns'] = '20' }

    return $config
}

function Resolve-WhenPredicate {
    param(
        [string]$Predicate,
        [Parameter(Mandatory)][string]$WorkDir
    )
    if (-not $Predicate -or $Predicate -eq 'true') { return $true }
    if ($Predicate -match '^exists\((.+)\)$') {
        return [bool](Test-Path (Join-Path $WorkDir $Matches[1]))
    }
    throw "Unknown when: predicate '$Predicate'"
}

function Get-ConfigBlock {
    param(
        [Parameter(Mandatory)][hashtable]$Config,
        [Parameter(Mandatory)][string]$Section,
        [Parameter(Mandatory)][string]$WorkDir
    )
    $entries = $Config[$Section]
    if ($entries -isnot [hashtable]) { return '' }
    # Old format: flat block key
    if ($entries.ContainsKey('block')) { return $entries['block'] }
    # New format: named sub-entries each with block + optional when
    $blocks = @()
    foreach ($key in ($entries.Keys | Sort-Object)) {
        $entry = $entries[$key]
        if ($entry -isnot [hashtable] -or -not $entry.ContainsKey('block')) { continue }
        if (Resolve-WhenPredicate -Predicate ($entry['when'] ?? '') -WorkDir $WorkDir) {
            $blocks += $entry['block']
        }
    }
    return ($blocks -join ' && ')
}
