BeforeAll {
    . "$PSScriptRoot/../lib/issue-lock.ps1"
}

Describe 'Get-IssueLockPath' {
    It 'returns the canonical path under .harness/locks' {
        $path = Get-IssueLockPath -RepoRoot '/repo' -IssueNumber 11
        ($path -replace '\\', '/') | Should -Be '/repo/.harness/locks/issue-11.lock'
    }
}

Describe 'Read-IssueLock' {
    It 'returns $null when no lock file exists' {
        $tmp = New-Item -ItemType Directory -Path "$env:TEMP/lock-test-$(New-Guid)"
        try {
            Read-IssueLock -RepoRoot $tmp.FullName -IssueNumber 1 | Should -Be $null
        } finally {
            Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
        }
    }

    It 'returns parsed hashtable when lock file is valid JSON' {
        $tmp = New-Item -ItemType Directory -Path "$env:TEMP/lock-test-$(New-Guid)"
        try {
            $lockDir = "$($tmp.FullName)/.harness/locks"
            New-Item -ItemType Directory -Path $lockDir -Force | Out-Null
            $body = '{"pid":99,"branch":"x","phase":"implement","acquired_at":"2026-05-12T15:00:00+08:00","machine":"H"}'
            Set-Content -Path "$lockDir/issue-1.lock" -Value $body -NoNewline

            $lock = Read-IssueLock -RepoRoot $tmp.FullName -IssueNumber 1
            $lock.pid    | Should -Be 99
            $lock.branch | Should -Be 'x'
            $lock.phase  | Should -Be 'implement'
        } finally {
            Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
        }
    }

    It 'returns $null on corrupt JSON (never throws)' {
        $tmp = New-Item -ItemType Directory -Path "$env:TEMP/lock-test-$(New-Guid)"
        try {
            $lockDir = "$($tmp.FullName)/.harness/locks"
            New-Item -ItemType Directory -Path $lockDir -Force | Out-Null
            Set-Content -Path "$lockDir/issue-1.lock" -Value '{not json' -NoNewline

            Read-IssueLock -RepoRoot $tmp.FullName -IssueNumber 1 | Should -Be $null
        } finally {
            Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

Describe 'Test-PidAlive' {
    It 'returns $false for invalid PIDs (<= 0)' {
        Test-PidAlive -PidValue 0   | Should -Be $false
        Test-PidAlive -PidValue -1  | Should -Be $false
    }

    It 'returns $true when the injected probe succeeds' {
        $result = Test-PidAlive -PidValue 100 -GetProcess { param($p) [pscustomobject]@{Id = $p} }
        $result | Should -Be $true
    }

    It 'returns $false when the injected probe throws' {
        $result = Test-PidAlive -PidValue 100 -GetProcess { param($p) throw 'not found' }
        $result | Should -Be $false
    }

    It 'returns $true for the current process PID' {
        Test-PidAlive -PidValue $PID | Should -Be $true
    }
}

Describe 'Invoke-AcquireIssueLock' {
    It 'creates a fresh lock file when none exists' {
        $tmp = New-Item -ItemType Directory -Path "$env:TEMP/lock-test-$(New-Guid)"
        try {
            $path = Invoke-AcquireIssueLock `
                -RepoRoot $tmp.FullName `
                -IssueNumber 4 `
                -BranchName 'issue-4-x' `
                -Phase 'implement' `
                -CurrentPid 42 `
                -Machine 'TESTBOX'

            Test-Path $path | Should -Be $true
            $lock = Get-Content $path -Raw | ConvertFrom-Json -AsHashtable
            $lock.pid     | Should -Be 42
            $lock.branch  | Should -Be 'issue-4-x'
            $lock.phase   | Should -Be 'implement'
            $lock.machine | Should -Be 'TESTBOX'
        } finally {
            Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
        }
    }

    It 'throws when an existing lock is held by a live PID and -Force is not set' {
        $tmp = New-Item -ItemType Directory -Path "$env:TEMP/lock-test-$(New-Guid)"
        try {
            # Seed an existing lock
            $null = Invoke-AcquireIssueLock `
                -RepoRoot $tmp.FullName `
                -IssueNumber 4 `
                -BranchName 'a' -Phase 'implement' `
                -CurrentPid 11 -Machine 'H' `
                -IsPidAlive { param($p) $true }

            {
                Invoke-AcquireIssueLock `
                    -RepoRoot $tmp.FullName `
                    -IssueNumber 4 `
                    -BranchName 'b' -Phase 'review' `
                    -CurrentPid 22 -Machine 'H' `
                    -IsPidAlive { param($p) $true }
            } | Should -Throw '*locked by PID 11*'
        } finally {
            Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
        }
    }

    It 'takes over a stale lock (dead PID) with a warning' {
        $tmp = New-Item -ItemType Directory -Path "$env:TEMP/lock-test-$(New-Guid)"
        try {
            $null = Invoke-AcquireIssueLock `
                -RepoRoot $tmp.FullName `
                -IssueNumber 4 `
                -BranchName 'a' -Phase 'implement' `
                -CurrentPid 11 -Machine 'H' `
                -IsPidAlive { param($p) $true }

            $path = Invoke-AcquireIssueLock `
                -RepoRoot $tmp.FullName `
                -IssueNumber 4 `
                -BranchName 'b' -Phase 'review' `
                -CurrentPid 22 -Machine 'H' `
                -IsPidAlive { param($p) $false } `
                -WarningAction SilentlyContinue

            $lock = Get-Content $path -Raw | ConvertFrom-Json -AsHashtable
            $lock.pid    | Should -Be 22
            $lock.branch | Should -Be 'b'
            $lock.phase  | Should -Be 'review'
        } finally {
            Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
        }
    }

    It 'forces takeover when -Force is set even with a live holder' {
        $tmp = New-Item -ItemType Directory -Path "$env:TEMP/lock-test-$(New-Guid)"
        try {
            $null = Invoke-AcquireIssueLock `
                -RepoRoot $tmp.FullName `
                -IssueNumber 4 `
                -BranchName 'a' -Phase 'implement' `
                -CurrentPid 11 -Machine 'H' `
                -IsPidAlive { param($p) $true }

            $path = Invoke-AcquireIssueLock `
                -RepoRoot $tmp.FullName `
                -IssueNumber 4 `
                -BranchName 'b' -Phase 'review' `
                -CurrentPid 22 -Machine 'H' `
                -Force `
                -IsPidAlive { param($p) $true } `
                -WarningAction SilentlyContinue

            $lock = Get-Content $path -Raw | ConvertFrom-Json -AsHashtable
            $lock.pid | Should -Be 22
        } finally {
            Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

Describe 'Invoke-ReleaseIssueLock' {
    It 'returns $false when no lock to release' {
        $tmp = New-Item -ItemType Directory -Path "$env:TEMP/lock-test-$(New-Guid)"
        try {
            Invoke-ReleaseIssueLock -RepoRoot $tmp.FullName -IssueNumber 99 | Should -Be $false
        } finally {
            Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
        }
    }

    It 'removes the lock file and returns $true' {
        $tmp = New-Item -ItemType Directory -Path "$env:TEMP/lock-test-$(New-Guid)"
        try {
            $null = Invoke-AcquireIssueLock `
                -RepoRoot $tmp.FullName `
                -IssueNumber 7 `
                -BranchName 'x' -Phase 'implement' `
                -CurrentPid 1 -Machine 'H'

            $path = Get-IssueLockPath -RepoRoot $tmp.FullName -IssueNumber 7
            Test-Path $path | Should -Be $true

            $result = Invoke-ReleaseIssueLock -RepoRoot $tmp.FullName -IssueNumber 7
            $result          | Should -Be $true
            Test-Path $path  | Should -Be $false
        } finally {
            Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

Describe 'Get-IssueLockList' {
    It 'returns empty when no locks directory exists' {
        $tmp = New-Item -ItemType Directory -Path "$env:TEMP/lock-test-$(New-Guid)"
        try {
            $result = @(Get-IssueLockList -RepoRoot $tmp.FullName)
            $result.Count | Should -Be 0
        } finally {
            Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
        }
    }

    It 'returns issue numbers extracted from lock filenames' {
        $tmp = New-Item -ItemType Directory -Path "$env:TEMP/lock-test-$(New-Guid)"
        try {
            $null = Invoke-AcquireIssueLock -RepoRoot $tmp.FullName -IssueNumber 3 -BranchName 'a' -Phase 'p' -CurrentPid 1 -Machine 'h'
            $null = Invoke-AcquireIssueLock -RepoRoot $tmp.FullName -IssueNumber 8 -BranchName 'b' -Phase 'p' -CurrentPid 2 -Machine 'h'

            $result = @(Get-IssueLockList -RepoRoot $tmp.FullName | Sort-Object)
            $result.Count | Should -Be 2
            $result[0]    | Should -Be 3
            $result[1]    | Should -Be 8
        } finally {
            Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}
