BeforeAll {
    $script:RunContent = Get-Content "$PSScriptRoot/../run.ps1" -Raw
}

Describe 'run.ps1 same-model pre-flight warning — centralised check' {
    It 'defines an Invoke-SameModelWarning or equivalent function/block' {
        # Must have a consolidated check covering all phases — not just implement vs review
        $script:RunContent | Should -Match 'Same.model.*warning|same.model.*check|Invoke-SameModelWarning|SameModelWarning'
    }

    It 'compares plan model against at least one other phase model' {
        # plan model var must appear in a comparison expression
        $script:RunContent | Should -Match 'planModel.*-eq|planModel.*-ne|-eq.*planModel|-ne.*planModel'
    }

    It 'compares implement model against review model' {
        $script:RunContent | Should -Match 'implementModel.*reviewModel|reviewModel.*implementModel'
    }

    It 'compares review model against merge model' {
        $script:RunContent | Should -Match 'reviewModel.*mergeModel|mergeModel.*reviewModel'
    }

    It 'issues WARNING for same-model phases' {
        $script:RunContent | Should -Match "WARNING.*same.model|same.model.*WARNING"
    }
}
