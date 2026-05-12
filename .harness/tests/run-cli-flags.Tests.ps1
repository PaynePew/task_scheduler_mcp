BeforeAll {
    $script:RunContent = Get-Content "$PSScriptRoot/../run.ps1" -Raw
}

Describe 'run.ps1 -StartPhase flag' {
    It 'declares -StartPhase parameter with implement/review/merge values' {
        $script:RunContent | Should -Match '\[string\]\$StartPhase'
        $script:RunContent | Should -Match "ValidateSet\(\s*'implement'\s*,\s*'review'\s*,\s*'merge'\s*\)"
    }

    It 'documents -StartPhase in the synopsis block' {
        $script:RunContent | Should -Match '\.PARAMETER StartPhase'
    }

    It 'skips implement docker invocation when StartPhase is not implement' {
        $script:RunContent | Should -Match "StartPhase\s+-ne\s+'implement'"
        $script:RunContent | Should -Match 'Implement phase skipped'
    }

    It 'skips review when StartPhase is merge' {
        $script:RunContent | Should -Match "StartPhase\s+-eq\s+'merge'"
    }

    It 'rehydrates worktree from remote branch when StartPhase != implement and none exists' {
        $script:RunContent | Should -Match 'New-IssueWorktreeFromRemoteBranch'
    }

    It 'rejects -StartPhase without -Issue early' {
        # Validation message includes the bad combo so the user can self-diagnose.
        $script:RunContent | Should -Match '-StartPhase\s+\$StartPhase\s+requires\s+-Issue'
    }
}

Describe 'run.ps1 CLI model override flags' {
    It 'declares -ImplementModel parameter' {
        $script:RunContent | Should -Match '\$ImplementModel'
    }

    It 'declares -PlanModel parameter' {
        $script:RunContent | Should -Match '\$PlanModel'
    }

    It 'declares -ReviewModel parameter' {
        $script:RunContent | Should -Match '\$ReviewModel'
    }

    It 'declares -MergeModel parameter' {
        $script:RunContent | Should -Match '\$MergeModel'
    }
}

Describe 'run.ps1 CLI max_turns override flags' {
    It 'declares -ImplementMaxTurns parameter' {
        $script:RunContent | Should -Match '\$ImplementMaxTurns'
    }

    It 'declares -PlanMaxTurns parameter' {
        $script:RunContent | Should -Match '\$PlanMaxTurns'
    }

    It 'declares -ReviewMaxTurns parameter' {
        $script:RunContent | Should -Match '\$ReviewMaxTurns'
    }

    It 'declares -MergeMaxTurns parameter' {
        $script:RunContent | Should -Match '\$MergeMaxTurns'
    }
}

Describe 'run.ps1 CLI override — precedence wiring' {
    It 'applies ImplementModel override to config after load' {
        $script:RunContent | Should -Match 'ImplementModel.*agents|agents.*ImplementModel'
    }

    It 'applies PlanModel override to config after load' {
        $script:RunContent | Should -Match 'PlanModel.*agents|agents.*PlanModel'
    }

    It 'applies ReviewModel override to config after load' {
        $script:RunContent | Should -Match 'ReviewModel.*agents|agents.*ReviewModel'
    }

    It 'applies MergeModel override to config after load' {
        $script:RunContent | Should -Match 'MergeModel.*agents|agents.*MergeModel'
    }

    It 'applies ImplementMaxTurns override to config after load' {
        $script:RunContent | Should -Match 'ImplementMaxTurns.*agents|agents.*ImplementMaxTurns'
    }

    It 'applies PlanMaxTurns override to config after load' {
        $script:RunContent | Should -Match 'PlanMaxTurns.*agents|agents.*PlanMaxTurns'
    }
}

Describe 'run.ps1 — config-driven docs paths' {
    # Regression guard: an earlier slice hardcoded "$RepoRoot/docs/adr" in the
    # plan phase, ignoring cfg.docs.adr_dir. That made the harness assume a
    # specific repo layout and silently injected an empty ADR list when the
    # config pointed elsewhere.
    It 'reads cfg.docs.adr_dir for the plan-phase ADR list (not hardcoded path)' {
        $script:RunContent | Should -Match 'cfg\.docs\.adr_dir|docs\.adr_dir'
        # Negative assertion: the old hardcoded literal must be gone.
        $script:RunContent | Should -Not -Match '"\$RepoRoot/docs/adr"'
    }
}
