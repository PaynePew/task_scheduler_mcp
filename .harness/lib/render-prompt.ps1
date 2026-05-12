function Invoke-RenderPrompt {
    param(
        [string]$Template,
        [hashtable]$Substitutions = @{}
    )

    $output = foreach ($line in (($Template -replace "`r`n", "`n") -split "`n")) {
        # A line that is solely a placeholder is droppable when its value is empty.
        # Genuine blank lines and mixed-content lines are always preserved.
        $isPlaceholderOnly = $line -match '^\s*\{\{[A-Z_0-9]+\}\}\s*$'
        foreach ($key in $Substitutions.Keys) {
            # Use .Replace (literal) so substitution values containing $1, $&, etc.
            # are not interpreted as regex back-references.
            $line = $line.Replace("{{$key}}", [string]$Substitutions[$key])
        }
        # Strip any remaining {{KEY}} placeholders (unmapped keys → empty).
        $line = $line -replace '\{\{[A-Z_0-9]+\}\}', ''
        if (-not $isPlaceholderOnly -or $line.Trim()) { $line }
    }
    ($output -join "`n")
}
