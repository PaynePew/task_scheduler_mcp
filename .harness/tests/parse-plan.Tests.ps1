BeforeAll {
    . "$PSScriptRoot/../lib/parse-plan.ps1"
}

Describe 'Invoke-ParsePlan' {
    It 'returns Plan for well-formed JSON with all required keys' {
        $content = '<plan>{"top":{"id":1,"title":"T","branch":"b","reason":"r","ac_count":2},"alternatives":[],"blocked":[]}</plan>'
        $result = Invoke-ParsePlan -Content $content
        $result.Plan          | Should -Not -BeNullOrEmpty
        $result.Plan.top.id   | Should -Be 1
        $result.Error         | Should -BeNullOrEmpty
    }

    It 'uses the last <plan> block when multiple are present' {
        $content = @'
<plan>{"top":{"id":1,"title":"first","branch":"b1","reason":"r","ac_count":1},"alternatives":[],"blocked":[]}</plan>
Some text between blocks.
<plan>{"top":{"id":2,"title":"second","branch":"b2","reason":"r","ac_count":3},"alternatives":[],"blocked":[]}</plan>
'@
        $result = Invoke-ParsePlan -Content $content
        $result.Plan.top.id    | Should -Be 2
        $result.Plan.top.title | Should -Be 'second'
    }

    It 'returns Error for malformed JSON' {
        $content = '<plan>{not valid json}</plan>'
        $result = Invoke-ParsePlan -Content $content
        $result.Error | Should -Not -BeNullOrEmpty
        $result.Plan  | Should -BeNullOrEmpty
    }

    It 'returns Error when top key is missing' {
        $content = '<plan>{"alternatives":[],"blocked":[]}</plan>'
        $result = Invoke-ParsePlan -Content $content
        $result.Error | Should -Match 'top'
    }

    It 'returns Error when alternatives key is missing' {
        $content = '<plan>{"top":{},"blocked":[]}</plan>'
        $result = Invoke-ParsePlan -Content $content
        $result.Error | Should -Match 'alternatives'
    }

    It 'returns Error when blocked key is missing' {
        $content = '<plan>{"top":{},"alternatives":[]}</plan>'
        $result = Invoke-ParsePlan -Content $content
        $result.Error | Should -Match 'blocked'
    }

    It 'extracts plan correctly from surrounding noise' {
        $content = @'
Some preamble thinking text here.
<plan>{"top":{"id":5,"title":"Clean","branch":"kanban-issue5","reason":"easy","ac_count":2},"alternatives":[],"blocked":[]}</plan>
More trailing text after the block.
'@
        $result = Invoke-ParsePlan -Content $content
        $result.Plan.top.id | Should -Be 5
    }

    It 'returns Error when no <plan> block is found' {
        $result = Invoke-ParsePlan -Content 'Just some text without a plan block.'
        $result.Error | Should -Not -BeNullOrEmpty
    }

    It 'returns the correct top.id when alternatives appear before top in JSON' {
        # JSON key ordering isn't guaranteed; verify ConvertFrom-Json reaches top.id directly.
        $content = '<plan>{"alternatives":[{"id":99,"title":"alt"}],"top":{"id":7,"title":"main","branch":"b","reason":"r","ac_count":1},"blocked":[]}</plan>'
        $result = Invoke-ParsePlan -Content $content
        $result.Plan.top.id | Should -Be 7
    }
}
