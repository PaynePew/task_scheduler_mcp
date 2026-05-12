BeforeAll {
    . "$PSScriptRoot/../lib/render-prompt.ps1"
}

Describe 'Invoke-RenderPrompt' {
    It 'substitutes a single placeholder' {
        Invoke-RenderPrompt -Template 'Hello {{NAME}}' -Substitutions @{ NAME = 'World' } |
            Should -Be 'Hello World'
    }

    It 'substitutes multiple placeholders on the same line' {
        Invoke-RenderPrompt -Template '{{A}} and {{B}}' -Substitutions @{ A = 'foo'; B = 'bar' } |
            Should -Be 'foo and bar'
    }

    It 'drops a line when the placeholder key is absent from substitutions' {
        $template = "keep`n{{MISSING}}`nkeep"
        Invoke-RenderPrompt -Template $template -Substitutions @{} |
            Should -Be "keep`nkeep"
    }

    It 'drops a line when a key maps to empty string' {
        $template = "before`n{{EMPTY}}`nafter"
        Invoke-RenderPrompt -Template $template -Substitutions @{ EMPTY = '' } |
            Should -Be "before`nafter"
    }

    It 'preserves lines that have no placeholders' {
        Invoke-RenderPrompt -Template 'no placeholders here' -Substitutions @{} |
            Should -Be 'no placeholders here'
    }

    It 'returns empty string for empty template' {
        Invoke-RenderPrompt -Template '' -Substitutions @{} |
            Should -Be ''
    }

    It 'treats substitution values as literals (no regex back-reference interpretation)' {
        # A value containing $1 / $& would corrupt under regex -replace; .Replace() is literal.
        Invoke-RenderPrompt -Template 'msg: {{V}}' -Substitutions @{ V = 'Closes #1, see $&' } |
            Should -Be 'msg: Closes #1, see $&'
    }

    It 'preserves genuine blank lines between content' {
        Invoke-RenderPrompt -Template "A`n`nB" -Substitutions @{} |
            Should -Be "A`n`nB"
    }

    It 'preserves a mixed-content line when its placeholder is empty' {
        # "Run tests with: " label survives even when {{TESTS_BLOCK}} resolves to empty.
        Invoke-RenderPrompt -Template "Run tests with: {{TESTS_BLOCK}}`nnext" -Substitutions @{ TESTS_BLOCK = '' } |
            Should -Be "Run tests with: `nnext"
    }
}
