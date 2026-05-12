Describe 'legacy harness files retired' {
    BeforeAll {
        $script:Root = (Resolve-Path "$PSScriptRoot/..").Path
        $script:RepoRoot = (Resolve-Path "$PSScriptRoot/../..").Path
    }

    It 'run-issue.ps1 is gone' {
        Test-Path (Join-Path $script:Root 'run-issue.ps1') | Should -BeFalse
    }

    It 'run-hello.ps1 is gone' {
        Test-Path (Join-Path $script:Root 'run-hello.ps1') | Should -BeFalse
    }

    It 'feature.md is gone' {
        Test-Path (Join-Path $script:Root 'feature.md') | Should -BeFalse
    }

    It '.sandcastle directory is gone' {
        Test-Path (Join-Path $script:RepoRoot '.sandcastle') | Should -BeFalse
    }
}
