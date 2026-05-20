function Test-ImageRebuildNeeded {
    param(
        [Parameter(Mandatory)][string]$DockerfilePath,
        [Parameter(Mandatory)][string]$MarkerPath,
        [string]$ImageName = ''
    )

    if (-not (Test-Path $DockerfilePath)) { return $true }
    if (-not (Test-Path $MarkerPath))     { return $true }

    # If an image name is supplied, verify it exists locally.
    # Covers the "operator ran `docker rmi` while the marker still matches" case.
    if ($ImageName -and -not (Test-ImageExists -ImageName $ImageName)) { return $true }

    $current = (Get-FileHash $DockerfilePath -Algorithm SHA256).Hash

    $stored = $null
    try { $stored = (Get-Content $MarkerPath -Raw -ErrorAction Stop).Trim() }
    catch { return $true }

    if ([string]::IsNullOrEmpty($stored)) { return $true }

    return ($current -ne $stored)
}

function Save-ImageHash {
    param(
        [Parameter(Mandatory)][string]$DockerfilePath,
        [Parameter(Mandatory)][string]$MarkerPath
    )
    $hash = (Get-FileHash $DockerfilePath -Algorithm SHA256).Hash
    Set-Content -Path $MarkerPath -Value $hash -NoNewline
}

function Test-ImageExists {
    param([Parameter(Mandatory)][string]$ImageName)
    $id = & docker images -q $ImageName 2>$null
    return [bool]$id
}

function Test-ImageToolchain {
    <#
    .SYNOPSIS
        Smoke-check that a built image actually contains the tools the Dockerfile
        is supposed to install. Returns $true if all checks pass, $false otherwise.
    .DESCRIPTION
        The Dockerfile hash check (Test-ImageRebuildNeeded) is not sufficient on
        its own: a previous `docker build` may have failed partway, the marker
        may have been hand-edited, or layers may have been pruned. This function
        runs the image and asserts critical binaries respond.

        Wired into run.ps1 so a corrupted/stale image triggers a rebuild even
        when the Dockerfile hash hasn't changed.
    #>
    param(
        [Parameter(Mandatory)][string]$ImageName,
        [string[]]$RequiredTools = @('uv', 'gh', 'claude', 'git')
    )

    foreach ($tool in $RequiredTools) {
        & docker run --rm --entrypoint $tool $ImageName --version *> $null 2>&1
        if ($LASTEXITCODE -ne 0) {
            Write-Host "  Image smoke check failed: '$tool --version' nonzero exit" -ForegroundColor Yellow
            return $false
        }
    }
    return $true
}
