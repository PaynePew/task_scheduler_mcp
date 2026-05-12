#!/usr/bin/env bats

setup() {
    source "$BATS_TEST_DIRNAME/../lib/render-prompt.sh"
}

@test "substitutes a single placeholder" {
    result=$(render_prompt 'Hello {{NAME}}' 'NAME=World')
    [ "$result" = "Hello World" ]
}

@test "substitutes multiple placeholders" {
    result=$(render_prompt '{{A}} and {{B}}' 'A=foo' 'B=bar')
    [ "$result" = "foo and bar" ]
}

@test "drops a line when placeholder key is absent" {
    template="$(printf 'keep\n{{MISSING}}\nkeep')"
    result=$(render_prompt "$template")
    [ "$result" = "$(printf 'keep\nkeep')" ]
}

@test "drops a line when key maps to empty string" {
    template="$(printf 'before\n{{EMPTY}}\nafter')"
    result=$(render_prompt "$template" 'EMPTY=')
    [ "$result" = "$(printf 'before\nafter')" ]
}

@test "preserves lines with no placeholders" {
    result=$(render_prompt 'no placeholders here')
    [ "$result" = "no placeholders here" ]
}

@test "returns empty string for empty template" {
    result=$(render_prompt '')
    [ "$result" = "" ]
}

@test "preserves genuine blank lines between content" {
    template="$(printf 'A\n\nB')"
    result=$(render_prompt "$template")
    [ "$result" = "$(printf 'A\n\nB')" ]
}

@test "preserves a line that is mixed content even when its placeholder is empty" {
    # The "Run tests with: " line keeps its label even when {{TESTS_BLOCK}} is empty.
    template="$(printf 'Run tests with: {{TESTS_BLOCK}}\nnext')"
    result=$(render_prompt "$template" 'TESTS_BLOCK=')
    [ "$result" = "$(printf 'Run tests with: \nnext')" ]
}
