BeforeAll {
    $script:RunScript  = "$PSScriptRoot/../run.ps1"
    $script:RunContent = Get-Content $script:RunScript -Raw
    . "$PSScriptRoot/../lib/load-config.ps1"
}

Describe 'run.ps1 merge phase — parameters' {
    It 'declares a -SkipMerge switch parameter' {
        $script:RunContent | Should -Match '\[switch\]\$SkipMerge'
    }

    It 'documents -SkipMerge in the synopsis block' {
        $script:RunContent | Should -Match 'SkipMerge'
    }
}

Describe 'run.ps1 merge phase — config wiring' {
    It 'reads agents.merge.model from config' {
        $script:RunContent | Should -Match "agents\.merge\.model|agents\['merge'\]"
    }

    It 'reads agents.merge.max_turns from config' {
        $script:RunContent | Should -Match "agents\.merge\.max_turns|agents\['merge'\]"
    }
}

Describe 'run.ps1 merge phase — separate docker run' {
    It 'contains at least three docker run invocation sites (implement, review, merge)' {
        $matches = [regex]::Matches($script:RunContent, "docker\s+@docker")
        $matches.Count | Should -BeGreaterOrEqual 3
    }

    It 'contains a merge phase step label' {
        $script:RunContent | Should -Match 'Merge phase|merge phase'
    }
}

Describe 'run.ps1 merge phase — final summary box' {
    It 'contains a unified final summary box header' {
        $script:RunContent | Should -Match 'Pipeline result|final summary'
    }

    It 'shows per-phase status with checkmark symbol for success' {
        $script:RunContent | Should -Match ([regex]::Escape('✓ COMPLETE'))
    }

    It 'shows per-phase status with cross symbol for failure' {
        $script:RunContent | Should -Match ([regex]::Escape('✗'))
    }

    It 'shows SKIPPED status for skipped phases' {
        $script:RunContent | Should -Match ([regex]::Escape('⊝ SKIPPED'))
    }

    It 'includes branch name in summary' {
        $script:RunContent | Should -Match 'branch'
    }

    It 'includes PR URL in summary when available' {
        $script:RunContent | Should -Match 'prUrl|PR'
    }

    It 'includes a next-step or resume command in summary' {
        $script:RunContent | Should -Match 'next|resume'
    }
}

Describe 'run.ps1 merge phase — mission verification' {
    # Regression: merge phase used to set $mergeOk = ($mergeExit -eq 0),
    # which marks ✓ COMPLETE even when the agent politely exits 0 without
    # opening a PR (e.g. it hit a HARD RULE and stopped). These checks
    # confirm the harness now gates success on actual PR creation.

    It 'requires both clean claude exit AND a PR URL before marking success' {
        # The success branch must check $prUrl, not just $mergeExit.
        $script:RunContent | Should -Match 'if\s*\(\s*\$prUrl\s*\)'
    }

    It 'falls back to `gh pr list` when the agent does not surface a PR URL in text' {
        $script:RunContent | Should -Match 'gh pr list[^\r\n]*--head'
    }

    It 'surfaces an explicit mission-incomplete message when claude exits 0 without a PR' {
        $script:RunContent | Should -Match 'no PR opened|mission incomplete'
    }
}
