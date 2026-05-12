BeforeAll {
    $script:PromptPath = "$PSScriptRoot/../prompts/implement.md"
    $script:Content    = Get-Content $script:PromptPath -Raw
}

Describe 'implement.md template' {
    $requiredKeys = @(
        'ISSUE', 'BRANCH',
        'DOCS_PRD_DIR', 'DOCS_CONTEXT', 'DOCS_ADR_DIR',
        'TESTS_BLOCK', 'TYPECHECK_BLOCK', 'COMMIT_STYLE'
    )

    It "contains placeholder {{<key>}} for each required substitution key" -ForEach ($requiredKeys | ForEach-Object { @{ Key = $_ } }) {
        $script:Content | Should -Match ([regex]::Escape("{{$($_.Key)}}"))
    }

    It 'instructs eager-loading the issue body' {
        $script:Content | Should -Match 'gh issue view'
    }

    It 'instructs posting a structured comment on COMPLETE or BLOCKED' {
        $script:Content | Should -Match 'gh issue comment'
    }

    It 'includes dirty-tree-on-resume handling contract' {
        $script:Content | Should -Match 'stash|WIP'
    }
}
