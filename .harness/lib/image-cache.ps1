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
