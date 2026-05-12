Describe 'agent-harness CI workflow' {
    BeforeAll {
        $script:WorkflowPath = "$PSScriptRoot/../../.github/workflows/agent-harness.yml"
    }

    It 'workflow file exists' {
        Test-Path $script:WorkflowPath | Should -BeTrue
    }

    It 'triggers on pull_request for .harness/** paths' {
        $content = Get-Content $script:WorkflowPath -Raw
        $content | Should -Match 'pull_request'
        $content | Should -Match '\.harness/'
    }

    It 'has a windows-latest runner job' {
        $content = Get-Content $script:WorkflowPath -Raw
        $content | Should -Match 'windows-latest'
    }

    It 'has an ubuntu-latest runner job' {
        $content = Get-Content $script:WorkflowPath -Raw
        $content | Should -Match 'ubuntu-latest'
    }

    It 'runs Pester on the windows job' {
        $content = Get-Content $script:WorkflowPath -Raw
        $content | Should -Match 'Pester|Invoke-Pester'
    }

    It 'runs bats on the ubuntu job' {
        $content = Get-Content $script:WorkflowPath -Raw
        $content | Should -Match 'bats'
    }
}
