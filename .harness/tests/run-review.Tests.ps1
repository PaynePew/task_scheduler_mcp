BeforeAll {
    $script:RunScript  = "$PSScriptRoot/../run.ps1"
    $script:RunContent = Get-Content $script:RunScript -Raw
    . "$PSScriptRoot/../lib/load-config.ps1"
}

Describe 'run.ps1 review phase — parameters' {
    It 'declares a -SkipReview switch parameter' {
        $script:RunContent | Should -Match '\[switch\]\$SkipReview'
    }

    It 'documents -SkipReview in the synopsis block' {
        $script:RunContent | Should -Match 'SkipReview'
    }
}

Describe 'run.ps1 review phase — same-model warning' {
    It 'emits a warning when implement model equals review model' {
        $script:RunContent | Should -Match 'implement.*model.*review.*model|review.*model.*implement.*model|same.model|Same.model'
    }
}

Describe 'run.ps1 review phase — separate docker run' {
    It 'contains at least two docker run invocation sites for implement and review' {
        $matches = [regex]::Matches($script:RunContent, "docker\s+@docker")
        $matches.Count | Should -BeGreaterOrEqual 2
    }

    It 'contains a review phase step label' {
        $script:RunContent | Should -Match "review"
    }
}

Describe 'run.ps1 review phase — config wiring' {
    It 'reads agents.review.model from config' {
        $script:RunContent | Should -Match "agents\.review\.model|agents\['review'\]"
    }

    It 'reads agents.review.max_turns from config' {
        $script:RunContent | Should -Match "agents\.review\.max_turns|agents\['review'\]"
    }
}
