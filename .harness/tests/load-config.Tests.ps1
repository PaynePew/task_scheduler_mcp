BeforeAll {
    . "$PSScriptRoot/../lib/load-config.ps1"
    $script:Fixtures = "$PSScriptRoot/fixtures"
}

Describe 'Import-HarnessConfig' {
    It 'loads a valid config and returns required keys' {
        $cfg = Import-HarnessConfig -ConfigPath "$script:Fixtures/valid-config.yml"
        $cfg.image        | Should -Be 'agent-harness:latest'
        $cfg.branch_prefix | Should -Be 'kanban-issue'
        $cfg.tracker.type | Should -Be 'github'
    }

    It 'applies default model when not specified' {
        $cfg = Import-HarnessConfig -ConfigPath "$script:Fixtures/minimal-config.yml"
        $cfg.defaults.model | Should -Be 'claude-sonnet-4-6'
    }

    It 'preserves explicit model override' {
        $cfg = Import-HarnessConfig -ConfigPath "$script:Fixtures/valid-config.yml"
        $cfg.defaults.model | Should -Be 'claude-opus-4-7'
    }

    It 'throws when image key is missing' {
        { Import-HarnessConfig -ConfigPath "$script:Fixtures/missing-image.yml" } |
            Should -Throw '*image*'
    }

    It 'throws when branch_prefix key is missing' {
        { Import-HarnessConfig -ConfigPath "$script:Fixtures/missing-branch-prefix.yml" } |
            Should -Throw '*branch_prefix*'
    }

    It 'throws when tracker.type key is missing' {
        { Import-HarnessConfig -ConfigPath "$script:Fixtures/missing-tracker-type.yml" } |
            Should -Throw '*tracker.type*'
    }

    It 'throws when tracker.type is not github' {
        { Import-HarnessConfig -ConfigPath "$script:Fixtures/invalid-tracker-type.yml" } |
            Should -Throw '*github*'
    }

    It 'throws when tracker.repo key is missing' {
        # run.ps1 calls `gh issue view --repo $cfg.tracker.repo` unconditionally;
        # surface the missing key here instead of as a confusing gh failure.
        { Import-HarnessConfig -ConfigPath "$script:Fixtures/missing-tracker-repo.yml" } |
            Should -Throw '*tracker.repo*'
    }

    It 'applies default agents.implement model and max_turns when not specified' {
        $cfg = Import-HarnessConfig -ConfigPath "$script:Fixtures/minimal-config.yml"
        $cfg.agents.implement.model     | Should -Be 'claude-sonnet-4-6'
        $cfg.agents.implement.max_turns | Should -Be '80'
    }

    It 'preserves explicit agents.implement overrides' {
        $cfg = Import-HarnessConfig -ConfigPath "$script:Fixtures/agents-config.yml"
        $cfg.agents.implement.model     | Should -Be 'claude-opus-4-7'
        $cfg.agents.implement.max_turns | Should -Be '120'
    }

    It 'applies default agents.plan model and max_turns when not specified' {
        $cfg = Import-HarnessConfig -ConfigPath "$script:Fixtures/minimal-config.yml"
        $cfg.agents.plan.model     | Should -Be 'claude-opus-4-7'
        $cfg.agents.plan.max_turns | Should -Be '10'
    }

    It 'preserves explicit agents.plan overrides' {
        $cfg = Import-HarnessConfig -ConfigPath "$script:Fixtures/agents-config.yml"
        $cfg.agents.plan.model     | Should -Be 'claude-haiku-4-5'
        $cfg.agents.plan.max_turns | Should -Be '5'
    }

    It 'applies default agents.review model and max_turns when not specified' {
        $cfg = Import-HarnessConfig -ConfigPath "$script:Fixtures/minimal-config.yml"
        $cfg.agents.review.model     | Should -Be 'claude-opus-4-7'
        $cfg.agents.review.max_turns | Should -Be '30'
    }

    It 'preserves explicit agents.review overrides' {
        $cfg = Import-HarnessConfig -ConfigPath "$script:Fixtures/agents-config.yml"
        $cfg.agents.review.model     | Should -Be 'claude-sonnet-4-6'
        $cfg.agents.review.max_turns | Should -Be '20'
    }

    It 'applies default agents.merge model and max_turns when not specified' {
        $cfg = Import-HarnessConfig -ConfigPath "$script:Fixtures/minimal-config.yml"
        $cfg.agents.merge.model     | Should -Be 'claude-sonnet-4-6'
        $cfg.agents.merge.max_turns | Should -Be '20'
    }

    It 'preserves explicit agents.merge overrides' {
        $cfg = Import-HarnessConfig -ConfigPath "$script:Fixtures/agents-config.yml"
        $cfg.agents.merge.model     | Should -Be 'claude-haiku-4-5'
        $cfg.agents.merge.max_turns | Should -Be '10'
    }

    It 'rejects tab indentation' {
        $tabFile = New-TemporaryFile
        Set-Content -Path $tabFile -Value "image: foo`nbranch_prefix: bar`ntracker:`n`ttype: github" -NoNewline
        try {
            { Import-HarnessConfig -ConfigPath $tabFile } | Should -Throw '*ab*'
        } finally {
            Remove-Item $tabFile -ErrorAction SilentlyContinue
        }
    }
}

Describe 'Resolve-WhenPredicate' {
    It 'returns true for absent/empty predicate (default)' {
        Resolve-WhenPredicate -Predicate '' -WorkDir $TestDrive | Should -Be $true
    }

    It 'returns true for predicate "true"' {
        Resolve-WhenPredicate -Predicate 'true' -WorkDir $TestDrive | Should -Be $true
    }

    It 'returns true for exists() when path is present' {
        $work = Join-Path $TestDrive 'has-backend'
        New-Item -ItemType Directory -Path "$work/backend" -Force | Out-Null
        Resolve-WhenPredicate -Predicate 'exists(backend)' -WorkDir $work | Should -Be $true
    }

    It 'returns false for exists() when path is absent' {
        $work = Join-Path $TestDrive 'no-backend'
        New-Item -ItemType Directory -Path $work -Force | Out-Null
        Resolve-WhenPredicate -Predicate 'exists(nonexistent)' -WorkDir $work | Should -Be $false
    }

    It 'throws for unknown predicate' {
        { Resolve-WhenPredicate -Predicate 'unknown()' -WorkDir $TestDrive } | Should -Throw '*predicate*'
    }
}

Describe 'Get-ConfigBlock' {
    It 'returns block directly for old-style flat tests entry (no when)' {
        $cfg = Import-HarnessConfig -ConfigPath "$script:Fixtures/minimal-config.yml"
        $cfg['tests'] = @{ block = 'pytest .' }
        Get-ConfigBlock -Config $cfg -Section 'tests' -WorkDir $TestDrive | Should -Be 'pytest .'
    }

    It 'returns empty string when section is absent' {
        $cfg = Import-HarnessConfig -ConfigPath "$script:Fixtures/minimal-config.yml"
        Get-ConfigBlock -Config $cfg -Section 'tests' -WorkDir $TestDrive | Should -Be ''
    }

    It 'includes entry without when: predicate unconditionally' {
        $cfg = Import-HarnessConfig -ConfigPath "$script:Fixtures/minimal-config.yml"
        $cfg['tests'] = @{
            root = @{ block = 'pytest .' }
        }
        Get-ConfigBlock -Config $cfg -Section 'tests' -WorkDir $TestDrive | Should -Be 'pytest .'
    }

    It 'includes entry when exists() predicate matches a present path' {
        $work = Join-Path $TestDrive 'has-backend'
        New-Item -ItemType Directory -Path "$work/backend" -Force | Out-Null
        $cfg = Import-HarnessConfig -ConfigPath "$script:Fixtures/minimal-config.yml"
        $cfg['tests'] = @{
            backend = @{ block = 'pytest backend/'; when = 'exists(backend)' }
        }
        Get-ConfigBlock -Config $cfg -Section 'tests' -WorkDir $work | Should -Be 'pytest backend/'
    }

    It 'excludes entry when exists() predicate path is absent' {
        $work = Join-Path $TestDrive 'empty-project'
        New-Item -ItemType Directory -Path $work -Force | Out-Null
        $cfg = Import-HarnessConfig -ConfigPath "$script:Fixtures/minimal-config.yml"
        $cfg['tests'] = @{
            backend = @{ block = 'pytest backend/'; when = 'exists(backend)' }
        }
        Get-ConfigBlock -Config $cfg -Section 'tests' -WorkDir $work | Should -Be ''
    }

    It 'combines multiple entries with && joining passing entries' {
        $work = Join-Path $TestDrive 'mixed-project'
        New-Item -ItemType Directory -Path "$work/backend" -Force | Out-Null
        $cfg = Import-HarnessConfig -ConfigPath "$script:Fixtures/minimal-config.yml"
        $cfg['tests'] = @{
            backend  = @{ block = 'pytest backend/'; when = 'exists(backend)' }
            frontend = @{ block = 'npm test'; when = 'exists(frontend)' }
            root     = @{ block = 'pytest .' }
        }
        $result = Get-ConfigBlock -Config $cfg -Section 'tests' -WorkDir $work
        $result | Should -Match 'pytest backend/'
        $result | Should -Match 'pytest \.'
        $result | Should -Not -Match 'npm test'
    }
}

Describe 'Flat-layout config — no when: predicates' {
    It 'loads flat-layout-config.yml without error' {
        { Import-HarnessConfig -ConfigPath "$script:Fixtures/flat-layout-config.yml" } | Should -Not -Throw
    }

    It 'flat-layout tests block resolves unconditionally for any workdir' {
        $cfg = Import-HarnessConfig -ConfigPath "$script:Fixtures/flat-layout-config.yml"
        $work = Join-Path $TestDrive 'flat-project'
        New-Item -ItemType Directory -Path $work -Force | Out-Null
        Get-ConfigBlock -Config $cfg -Section 'tests' -WorkDir $work | Should -Be 'pytest .'
    }

    It 'flat-layout typecheck block resolves unconditionally for any workdir' {
        $cfg = Import-HarnessConfig -ConfigPath "$script:Fixtures/flat-layout-config.yml"
        $work = Join-Path $TestDrive 'flat-project2'
        New-Item -ItemType Directory -Path $work -Force | Out-Null
        Get-ConfigBlock -Config $cfg -Section 'typecheck' -WorkDir $work | Should -Be 'mypy .'
    }
}
