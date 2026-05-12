#!/usr/bin/env bats

setup() {
    source "$BATS_TEST_DIRNAME/../lib/parse-plan.sh"
}

@test "returns plan JSON for well-formed content" {
    local content='<plan>{"top":{"id":1,"title":"T","branch":"b","reason":"r","ac_count":2},"alternatives":[],"blocked":[]}</plan>'
    run parse_plan "$content"
    [ "$status" -eq 0 ]
    [[ "$output" == *'"top"'* ]]
    [[ "$output" == *'"id":1'* ]]
}

@test "uses last plan block when multiple are present" {
    local content='<plan>{"top":{"id":1,"title":"first","branch":"b1","reason":"r","ac_count":1},"alternatives":[],"blocked":[]}</plan>
<plan>{"top":{"id":2,"title":"second","branch":"b2","reason":"r","ac_count":3},"alternatives":[],"blocked":[]}</plan>'
    run parse_plan "$content"
    [ "$status" -eq 0 ]
    [[ "$output" == *'"id":2'* ]]
    [[ "$output" == *'"second"'* ]]
}

@test "fails and prints error when no plan block found" {
    run parse_plan "Just some text without a plan block."
    [ "$status" -ne 0 ]
}

@test "fails when top key is missing" {
    local content='<plan>{"alternatives":[],"blocked":[]}</plan>'
    run parse_plan "$content"
    [ "$status" -ne 0 ]
}

@test "fails when alternatives key is missing" {
    local content='<plan>{"top":{},"blocked":[]}</plan>'
    run parse_plan "$content"
    [ "$status" -ne 0 ]
}

@test "extracts plan from surrounding noise" {
    local content='Some preamble thinking text.
<plan>{"top":{"id":5,"title":"Clean","branch":"kanban-issue5","reason":"easy","ac_count":2},"alternatives":[],"blocked":[]}</plan>
More trailing text.'
    run parse_plan "$content"
    [ "$status" -eq 0 ]
    [[ "$output" == *'"id":5'* ]]
}

@test "parse_plan_top_id extracts top.id even when alternatives appear first" {
    local content='<plan>{"alternatives":[{"id":99,"title":"alt","branch":"x","reason":"y"}],"top":{"id":7,"title":"main","branch":"b","reason":"r","ac_count":1},"blocked":[]}</plan>'
    local json
    json=$(parse_plan "$content")
    [ "$(parse_plan_top_id "$json")" = "7" ]
}

@test "parse_plan_top_field extracts a string scalar from top" {
    local content='<plan>{"alternatives":[],"top":{"id":7,"title":"main","branch":"kanban-issue7-foo","reason":"r","ac_count":1},"blocked":[]}</plan>'
    local json
    json=$(parse_plan "$content")
    [ "$(parse_plan_top_field branch "$json")" = "kanban-issue7-foo" ]
}
