BeforeAll {
    $script:RunContent = Get-Content "$PSScriptRoot/../run.ps1" -Raw
}

Describe 'run.ps1 lifecycle hooks — declaration' {
    It 'defines an Invoke-HarnessHook function' {
        $script:RunContent | Should -Match 'function Invoke-HarnessHook'
    }

    It 'sets HARNESS_ISSUE env var before calling hook' {
        $script:RunContent | Should -Match 'HARNESS_ISSUE'
    }

    It 'sets HARNESS_BRANCH env var before calling hook' {
        $script:RunContent | Should -Match 'HARNESS_BRANCH'
    }

    It 'sets HARNESS_PHASE env var before calling hook' {
        $script:RunContent | Should -Match 'HARNESS_PHASE'
    }

    It 'invokes before-tests hook' {
        $script:RunContent | Should -Match 'before-tests'
    }

    It 'invokes after-implement hook' {
        $script:RunContent | Should -Match 'after-implement'
    }

    It 'hook non-zero exit logs warning but does not stop pipeline' {
        $script:RunContent | Should -Match 'WARNING.*hook|hook.*warning' -Because 'non-zero hook exit must only warn'
    }
}

Describe 'run.ps1 lifecycle hooks — behaviour' {
    It 'skips hook when hook file does not exist' {
        $script:RunContent | Should -Match 'Test-Path.*hook|hook.*Test-Path'
    }
}

Describe 'run.ps1 lifecycle hooks — demo fixture' {
    It 'before-tests.sh fixture exists' {
        "$PSScriptRoot/../tests/fixtures/hooks-demo/before-tests.sh" | Should -Exist
    }

    It 'after-implement.sh fixture exists' {
        "$PSScriptRoot/../tests/fixtures/hooks-demo/after-implement.sh" | Should -Exist
    }

    It 'before-tests.sh echoes HARNESS env vars' {
        $content = Get-Content "$PSScriptRoot/../tests/fixtures/hooks-demo/before-tests.sh" -Raw
        $content | Should -Match 'HARNESS_ISSUE|HARNESS_BRANCH|HARNESS_PHASE'
    }

    It 'after-implement.sh echoes HARNESS env vars' {
        $content = Get-Content "$PSScriptRoot/../tests/fixtures/hooks-demo/after-implement.sh" -Raw
        $content | Should -Match 'HARNESS_ISSUE|HARNESS_BRANCH|HARNESS_PHASE'
    }
}
