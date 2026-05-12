BeforeAll {
    . "$PSScriptRoot/../lib/worktree.ps1"
}

Describe 'Get-IssueWorktreePath' {
    It 'returns the canonical path under .harness/worktrees' {
        $path = Get-IssueWorktreePath -RepoRoot '/repo' -IssueNumber 42
        # Normalise separators for cross-platform comparison
        ($path -replace '\\', '/') | Should -Be '/repo/.harness/worktrees/issue-42'
    }
}

Describe 'Test-IssueWorktreeExists' {
    It 'returns $false when no worktree directory exists' {
        $tmp = New-Item -ItemType Directory -Path "$env:TEMP/wt-test-$(New-Guid)"
        try {
            Test-IssueWorktreeExists -RepoRoot $tmp.FullName -IssueNumber 1 | Should -Be $false
        } finally {
            Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
        }
    }

    It 'returns $true when the worktree directory exists' {
        $tmp = New-Item -ItemType Directory -Path "$env:TEMP/wt-test-$(New-Guid)"
        try {
            New-Item -ItemType Directory -Path "$($tmp.FullName)/.harness/worktrees/issue-7" -Force | Out-Null
            Test-IssueWorktreeExists -RepoRoot $tmp.FullName -IssueNumber 7 | Should -Be $true
        } finally {
            Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

Describe 'New-IssueWorktree' {
    It 'invokes clone + remote-rewrite + branch checkout in order' {
        $tmp = New-Item -ItemType Directory -Path "$env:TEMP/wt-test-$(New-Guid)"
        try {
            $script:cloneCall    = $null
            $script:setUrlCall   = $null
            $script:checkoutCall = $null

            $cloneMock = { param($repo, $path)
                $script:cloneCall = @{ repo = $repo; path = $path }
                New-Item -ItemType Directory -Path $path -Force | Out-Null
            }
            $getUrlMock = { param($repo) return 'https://github.com/owner/repo.git' }
            $setUrlMock = { param($path, $url)
                $script:setUrlCall = @{ path = $path; url = $url }
            }
            $checkoutMock = { param($path, $branch, $base)
                $script:checkoutCall = @{ path = $path; branch = $branch; base = $base }
            }

            $result = New-IssueWorktree `
                -RepoRoot $tmp.FullName `
                -IssueNumber 3 `
                -BranchName 'issue-3-foo' `
                -BaseBranch 'origin/main' `
                -GitClone          $cloneMock `
                -GitGetOriginUrl   $getUrlMock `
                -GitSetOriginUrl   $setUrlMock `
                -GitCheckoutBranch $checkoutMock

            ($result -replace '\\', '/') | Should -Be "$($tmp.FullName -replace '\\', '/')/.harness/worktrees/issue-3"
            $script:cloneCall.repo            | Should -Be $tmp.FullName
            $script:setUrlCall.url            | Should -Be 'https://github.com/owner/repo.git'
            $script:checkoutCall.branch       | Should -Be 'issue-3-foo'
            $script:checkoutCall.base         | Should -Be 'origin/main'
            Test-Path "$($tmp.FullName)/.harness/worktrees" | Should -Be $true
        } finally {
            Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
        }
    }

    It 'returns a single string path even when scriptblocks write to stdout' {
        # Regression: a previous version of the default git scriptblocks let
        # `git`'s stdout leak into the function's output stream, so the
        # returned $worktreePath was actually an array of lines. Callers
        # then interpolated that array (joined by spaces) into docker
        # --volume arguments, which broke with "too many colons".
        $tmp = New-Item -ItemType Directory -Path "$env:TEMP/wt-test-$(New-Guid)"
        try {
            $noisyClone = { param($r, $p)
                Write-Output "Cloning into '$p'..."
                Write-Output "remote: Enumerating objects: 1234, done."
                New-Item -ItemType Directory -Path $p -Force | Out-Null
            }
            $noisyGetUrl = { param($r)
                # get-url returns a single line in real life; the trim in
                # production protects against trailing newlines, but here
                # we keep the fixture simple
                return 'https://github.com/owner/repo.git'
            }
            $noisySetUrl = { param($p, $u)
                Write-Output "Updating origin to $u"
            }
            $noisyCheckout = { param($p, $b, $base)
                Write-Output "Switched to a new branch '$b'"
            }

            $result = New-IssueWorktree `
                -RepoRoot $tmp.FullName `
                -IssueNumber 11 `
                -BranchName 'issue-11-noisy' `
                -GitClone          $noisyClone `
                -GitGetOriginUrl   $noisyGetUrl `
                -GitSetOriginUrl   $noisySetUrl `
                -GitCheckoutBranch $noisyCheckout

            $result          | Should -BeOfType [string]
            ($result -replace '\\', '/') | Should -Be "$($tmp.FullName -replace '\\', '/')/.harness/worktrees/issue-11"
        } finally {
            Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
        }
    }

    It 'skips set-url when origin URL is empty' {
        # Defensive: if the source repo has no origin remote, we still want
        # clone+checkout to succeed rather than crash on set-url.
        $tmp = New-Item -ItemType Directory -Path "$env:TEMP/wt-test-$(New-Guid)"
        try {
            $script:setUrlCalled = $false
            $cloneMock    = { param($r, $p) New-Item -ItemType Directory -Path $p -Force | Out-Null }
            $emptyUrlMock = { param($r) return '' }
            $setUrlMock   = { param($p, $u) $script:setUrlCalled = $true }
            $checkoutMock = { param($p, $b, $base) }

            $null = New-IssueWorktree `
                -RepoRoot $tmp.FullName `
                -IssueNumber 8 `
                -BranchName 'issue-8-no-origin' `
                -GitClone          $cloneMock `
                -GitGetOriginUrl   $emptyUrlMock `
                -GitSetOriginUrl   $setUrlMock `
                -GitCheckoutBranch $checkoutMock

            $script:setUrlCalled | Should -Be $false
        } finally {
            Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
        }
    }

    It 'throws when worktree directory already exists' {
        $tmp = New-Item -ItemType Directory -Path "$env:TEMP/wt-test-$(New-Guid)"
        try {
            New-Item -ItemType Directory -Path "$($tmp.FullName)/.harness/worktrees/issue-5" -Force | Out-Null
            {
                New-IssueWorktree `
                    -RepoRoot $tmp.FullName `
                    -IssueNumber 5 `
                    -BranchName 'issue-5-x' `
                    -GitClone          { param($r, $p) } `
                    -GitGetOriginUrl   { param($r) '' } `
                    -GitSetOriginUrl   { param($p, $u) } `
                    -GitCheckoutBranch { param($p, $b, $base) }
            } | Should -Throw '*already exists*'
        } finally {
            Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

Describe 'New-IssueWorktreeFromRemoteBranch' {
    It 'invokes clone + remote-rewrite + fetch + tracking-checkout in order' {
        $tmp = New-Item -ItemType Directory -Path "$env:TEMP/wt-test-$(New-Guid)"
        try {
            $script:cloneCall    = $null
            $script:setUrlCall   = $null
            $script:fetchCall    = $null
            $script:checkoutCall = $null

            $cloneMock = { param($repo, $path)
                $script:cloneCall = @{ repo = $repo; path = $path }
                New-Item -ItemType Directory -Path $path -Force | Out-Null
            }
            $getUrlMock   = { param($repo) return 'https://github.com/owner/repo.git' }
            $setUrlMock   = { param($path, $url) $script:setUrlCall = @{ path = $path; url = $url } }
            $fetchMock    = { param($path) $script:fetchCall = @{ path = $path } }
            $checkoutMock = { param($path, $branch) $script:checkoutCall = @{ path = $path; branch = $branch } }

            $result = New-IssueWorktreeFromRemoteBranch `
                -RepoRoot $tmp.FullName `
                -IssueNumber 14 `
                -BranchName 'issue-14-foo' `
                -GitClone            $cloneMock `
                -GitGetOriginUrl     $getUrlMock `
                -GitSetOriginUrl     $setUrlMock `
                -GitFetch            $fetchMock `
                -GitCheckoutTracking $checkoutMock

            ($result -replace '\\', '/') | Should -Be "$($tmp.FullName -replace '\\', '/')/.harness/worktrees/issue-14"
            $script:cloneCall.repo      | Should -Be $tmp.FullName
            $script:setUrlCall.url      | Should -Be 'https://github.com/owner/repo.git'
            $script:fetchCall           | Should -Not -BeNullOrEmpty
            $script:checkoutCall.branch | Should -Be 'issue-14-foo'
        } finally {
            Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
        }
    }

    It 'throws when worktree directory already exists' {
        $tmp = New-Item -ItemType Directory -Path "$env:TEMP/wt-test-$(New-Guid)"
        try {
            New-Item -ItemType Directory -Path "$($tmp.FullName)/.harness/worktrees/issue-15" -Force | Out-Null
            {
                New-IssueWorktreeFromRemoteBranch `
                    -RepoRoot $tmp.FullName `
                    -IssueNumber 15 `
                    -BranchName 'issue-15-x' `
                    -GitClone            { param($r, $p) } `
                    -GitGetOriginUrl     { param($r) '' } `
                    -GitSetOriginUrl     { param($p, $u) } `
                    -GitFetch            { param($p) } `
                    -GitCheckoutTracking { param($p, $b) }
            } | Should -Throw '*already exists*'
        } finally {
            Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
        }
    }

    It 'surfaces a helpful error when checkout fails because remote branch is missing' {
        $tmp = New-Item -ItemType Directory -Path "$env:TEMP/wt-test-$(New-Guid)"
        try {
            $failingCheckout = { param($p, $b)
                throw "git checkout $b failed (exit 1) — does origin/$b exist?"
            }
            $cloneMock = { param($r, $p) New-Item -ItemType Directory -Path $p -Force | Out-Null }

            {
                New-IssueWorktreeFromRemoteBranch `
                    -RepoRoot $tmp.FullName `
                    -IssueNumber 16 `
                    -BranchName 'issue-16-nonexistent' `
                    -GitClone            $cloneMock `
                    -GitGetOriginUrl     { param($r) '' } `
                    -GitSetOriginUrl     { param($p, $u) } `
                    -GitFetch            { param($p) } `
                    -GitCheckoutTracking $failingCheckout
            } | Should -Throw '*does origin/issue-16-nonexistent exist*'
        } finally {
            Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

Describe 'Resume-IssueWorktree' {
    It 'throws when worktree does not exist' {
        $tmp = New-Item -ItemType Directory -Path "$env:TEMP/wt-test-$(New-Guid)"
        try {
            { Resume-IssueWorktree -RepoRoot $tmp.FullName -IssueNumber 9 } |
                Should -Throw '*no worktree*'
        } finally {
            Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
        }
    }

    It 'throws when worktree exists but .git is missing (broken state)' {
        $tmp = New-Item -ItemType Directory -Path "$env:TEMP/wt-test-$(New-Guid)"
        try {
            New-Item -ItemType Directory -Path "$($tmp.FullName)/.harness/worktrees/issue-9" -Force | Out-Null
            { Resume-IssueWorktree -RepoRoot $tmp.FullName -IssueNumber 9 } |
                Should -Throw '*broken*'
        } finally {
            Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
        }
    }

    It 'returns the path when .git is a directory (clone-based)' {
        $tmp = New-Item -ItemType Directory -Path "$env:TEMP/wt-test-$(New-Guid)"
        try {
            $wt = "$($tmp.FullName)/.harness/worktrees/issue-9"
            New-Item -ItemType Directory -Path "$wt/.git" -Force | Out-Null

            $result = Resume-IssueWorktree -RepoRoot $tmp.FullName -IssueNumber 9
            ($result -replace '\\', '/') | Should -Be ($wt -replace '\\', '/')
        } finally {
            Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
        }
    }

    It 'returns the path when .git is a file (legacy worktree pointer)' {
        # Back-compat: pre-clone-fix harness versions used real git worktrees
        # where .git is a pointer file. Resume should still accept those so
        # the user can finish an in-progress slice through the transition.
        $tmp = New-Item -ItemType Directory -Path "$env:TEMP/wt-test-$(New-Guid)"
        try {
            $wt = "$($tmp.FullName)/.harness/worktrees/issue-10"
            New-Item -ItemType Directory -Path $wt -Force | Out-Null
            Set-Content -Path "$wt/.git" -Value 'gitdir: /fake/path' -Encoding UTF8

            $result = Resume-IssueWorktree -RepoRoot $tmp.FullName -IssueNumber 10
            ($result -replace '\\', '/') | Should -Be ($wt -replace '\\', '/')
        } finally {
            Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

Describe 'Remove-IssueWorktree' {
    It 'returns $false when nothing to remove' {
        $tmp = New-Item -ItemType Directory -Path "$env:TEMP/wt-test-$(New-Guid)"
        try {
            $result = Remove-IssueWorktree `
                -RepoRoot $tmp.FullName `
                -IssueNumber 99 `
                -RemoveDirectory { param($p) }
            $result | Should -Be $false
        } finally {
            Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
        }
    }

    It 'removes the directory and returns $true when worktree exists' {
        $tmp = New-Item -ItemType Directory -Path "$env:TEMP/wt-test-$(New-Guid)"
        try {
            $wt = "$($tmp.FullName)/.harness/worktrees/issue-4"
            New-Item -ItemType Directory -Path $wt -Force | Out-Null

            $script:removedPath = $null
            $result = Remove-IssueWorktree `
                -RepoRoot $tmp.FullName `
                -IssueNumber 4 `
                -RemoveDirectory { param($p) $script:removedPath = $p }

            $result              | Should -Be $true
            ($script:removedPath -replace '\\', '/') | Should -Be ($wt -replace '\\', '/')
        } finally {
            Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
        }
    }

    It 'real removal: deletes a .git-containing directory without git worktree registration' {
        # Regression: previous implementation called `git worktree remove`,
        # which fails when the .git/ became a real repo (agent-ran-git-init
        # case). The clone-based removal is just filesystem delete and
        # should always work.
        $tmp = New-Item -ItemType Directory -Path "$env:TEMP/wt-test-$(New-Guid)"
        try {
            $wt = "$($tmp.FullName)/.harness/worktrees/issue-77"
            New-Item -ItemType Directory -Path "$wt/.git/objects" -Force | Out-Null
            New-Item -ItemType File -Path "$wt/README.md" -Force | Out-Null

            $result = Remove-IssueWorktree -RepoRoot $tmp.FullName -IssueNumber 77
            $result | Should -Be $true
            Test-Path $wt | Should -Be $false
        } finally {
            Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

Describe 'Get-IssueWorktreeList' {
    It 'returns empty array when no worktrees directory exists' {
        $tmp = New-Item -ItemType Directory -Path "$env:TEMP/wt-test-$(New-Guid)"
        try {
            $result = Get-IssueWorktreeList -RepoRoot $tmp.FullName
            @($result).Count | Should -Be 0
        } finally {
            Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
        }
    }

    It 'returns sorted list of issue numbers extracted from directory names' {
        $tmp = New-Item -ItemType Directory -Path "$env:TEMP/wt-test-$(New-Guid)"
        try {
            New-Item -ItemType Directory -Path "$($tmp.FullName)/.harness/worktrees/issue-4" -Force | Out-Null
            New-Item -ItemType Directory -Path "$($tmp.FullName)/.harness/worktrees/issue-12" -Force | Out-Null
            New-Item -ItemType Directory -Path "$($tmp.FullName)/.harness/worktrees/issue-7" -Force | Out-Null
            # Non-issue directory should be ignored
            New-Item -ItemType Directory -Path "$($tmp.FullName)/.harness/worktrees/garbage" -Force | Out-Null

            $result = @(Get-IssueWorktreeList -RepoRoot $tmp.FullName | Sort-Object)
            $result.Count | Should -Be 3
            $result[0]    | Should -Be 4
            $result[1]    | Should -Be 7
            $result[2]    | Should -Be 12
        } finally {
            Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}
