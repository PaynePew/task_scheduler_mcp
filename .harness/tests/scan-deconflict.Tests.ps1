BeforeAll {
    . "$PSScriptRoot/../lib/scan-deconflict.ps1"
}

Describe 'Get-DeconflictExclusions' {
    It 'parses a well-formed branch name and returns its issue number' {
        $result = Get-DeconflictExclusions -BranchPrefix 'kanban-issue' `
            -LocalBranches @('kanban-issue42-my-feature') `
            -GhPrListJson '[]'
        $result | Should -Contain 42
    }

    It 'skips malformed branch names without crashing' {
        $result = Get-DeconflictExclusions -BranchPrefix 'kanban-issue' `
            -LocalBranches @('main', 'feat/no-issue-here', 'kanban-issueBAD-x') `
            -GhPrListJson '[]'
        $result.Count | Should -Be 0
    }

    It 'skips branch missing trailing separator without crashing' {
        $result = Get-DeconflictExclusions -BranchPrefix 'kanban-issue' `
            -LocalBranches @('kanban-issue42') `
            -GhPrListJson '[]'
        $result.Count | Should -Be 0
    }

    It 'excludes issue numbers referenced by open PRs' {
        $prJson = '[{"number":1,"headRefName":"kanban-issue7-some-pr"}]'
        $result = Get-DeconflictExclusions -BranchPrefix 'kanban-issue' `
            -LocalBranches @() `
            -GhPrListJson $prJson
        $result | Should -Contain 7
    }

    It 'falls back gracefully to local-only when gh data is empty' {
        $result = Get-DeconflictExclusions -BranchPrefix 'kanban-issue' `
            -LocalBranches @('kanban-issue10-local-only') `
            -GhPrListJson ''
        $result | Should -Contain 10
    }

    It 'returns empty set when no matching branches or PRs' {
        $result = Get-DeconflictExclusions -BranchPrefix 'kanban-issue' `
            -LocalBranches @() `
            -GhPrListJson '[]'
        $result.Count | Should -Be 0
    }

    It 'deduplicates when local branch and PR reference the same issue' {
        $prJson = '[{"number":1,"headRefName":"kanban-issue42-pr-branch"}]'
        $result = Get-DeconflictExclusions -BranchPrefix 'kanban-issue' `
            -LocalBranches @('kanban-issue42-local-branch') `
            -GhPrListJson $prJson
        $result.Count | Should -Be 1
        $result | Should -Contain 42
    }

    It 'collects multiple distinct issue numbers' {
        $prJson = '[{"number":1,"headRefName":"kanban-issue7-pr"}]'
        $result = Get-DeconflictExclusions -BranchPrefix 'kanban-issue' `
            -LocalBranches @('kanban-issue42-local', 'kanban-issue99-other') `
            -GhPrListJson $prJson
        $result | Should -Contain 42
        $result | Should -Contain 99
        $result | Should -Contain 7
    }

    It 'matches remote-tracking refs by stripping the remote prefix' {
        $result = Get-DeconflictExclusions -BranchPrefix 'kanban-issue' `
            -LocalBranches @('origin/kanban-issue42-remote-feature') `
            -GhPrListJson '[]'
        $result | Should -Contain 42
    }
}
