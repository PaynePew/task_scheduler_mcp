BeforeAll {
    $script:PromptPath = "$PSScriptRoot/../prompts/merge.md"
    $script:Content    = Get-Content $script:PromptPath -Raw
}

Describe 'merge.md template — token coverage' {
    $requiredKeys = @('BRANCH', 'ISSUE', 'REPO', 'TESTS_BLOCK')

    It "contains placeholder {{<key>}} for each required substitution key" -ForEach ($requiredKeys | ForEach-Object { @{ Key = $_ } }) {
        $script:Content | Should -Match ([regex]::Escape("{{$($_.Key)}}"))
    }
}

Describe 'merge.md template — merge actions' {
    It 'instructs git push -u origin' {
        $script:Content | Should -Match 'git push -u origin'
    }

    It 'instructs gh pr create' {
        $script:Content | Should -Match 'gh pr create'
    }

    It 'PR body contains Closes #{{ISSUE}}' {
        $script:Content | Should -Match ([regex]::Escape('Closes #{{ISSUE}}'))
    }

    It 'instructs commenting on the issue via gh issue comment' {
        $script:Content | Should -Match 'gh issue comment'
    }

    It 'comment mentions PR opened and ready for human review' {
        $script:Content | Should -Match 'ready for human review'
    }
}

Describe 'merge.md template — hard rules' {
    It 'forbids git merge to main' {
        $script:Content | Should -Match 'NOT.*git merge|git merge.*NOT|Do NOT.*merge'
    }

    It 'forbids gh issue close' {
        $script:Content | Should -Match 'NOT.*gh issue close|gh issue close.*NOT|Do NOT.*close'
    }

    It 'forbids --auto-merge' {
        $script:Content | Should -Match 'NOT.*auto-merge|auto-merge.*NOT|Do NOT.*auto-merge'
    }

    It 'forbids squash and rebase' {
        $script:Content | Should -Match 'squash|rebase'
    }

    It 'includes promise-COMPLETE exit marker' {
        $script:Content | Should -Match ([regex]::Escape('<promise>COMPLETE'))
    }
}
