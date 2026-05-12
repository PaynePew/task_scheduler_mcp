BeforeAll {
    $script:RunContent = Get-Content "$PSScriptRoot/../run.ps1" -Raw
}

Describe 'run.ps1 — gh token forwarded to container' {
    It 'pre-flight populates $env:GH_TOKEN from `gh auth token` when unset' {
        $script:RunContent | Should -Match 'gh auth token'
        $script:RunContent | Should -Match '\$env:GH_TOKEN'
    }

    It 'passes --env GH_TOKEN to docker for all 4 phases (plan, implement, review, merge)' {
        $matches = [regex]::Matches($script:RunContent, "'--env',\s*'GH_TOKEN'")
        $matches.Count | Should -BeGreaterOrEqual 4
    }
}

Describe 'run.ps1 — claude headless permission mode' {
    It 'passes --permission-mode bypassPermissions to claude in all 4 phases' {
        $matches = [regex]::Matches($script:RunContent, '--permission-mode bypassPermissions')
        $matches.Count | Should -BeGreaterOrEqual 4
    }
}

Describe 'run.ps1 — pipeline summary label' {
    It 'labels smoke-test runs as smoke-test, not implement' {
        # The summary box should display 'smoke-test' when -SmokeTest is set,
        # 'implement' otherwise. Look for a conditional label assignment.
        $script:RunContent | Should -Match "SmokeTest.*'smoke-test'|'smoke-test'.*SmokeTest"
    }
}
