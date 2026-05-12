#!/usr/bin/env bats

setup() {
    source "$BATS_TEST_DIRNAME/../lib/scan-deconflict.sh"
    unset SCAN_MOCK_BRANCHES
}

@test "parses branch name and returns issue number" {
    export SCAN_MOCK_BRANCHES="  kanban-issue42-my-feature"
    run scan_deconflict "kanban-issue" ""
    [ "$status" -eq 0 ]
    [[ "$output" == *"42"* ]]
}

@test "skips malformed branch names without crashing" {
    export SCAN_MOCK_BRANCHES="  main
  feat/no-issue-here
  kanban-issueBAD-x"
    run scan_deconflict "kanban-issue" ""
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "skips branch missing trailing separator without crashing" {
    export SCAN_MOCK_BRANCHES="  kanban-issue42"
    run scan_deconflict "kanban-issue" ""
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "excludes issue numbers from open PR JSON" {
    export SCAN_MOCK_BRANCHES=""
    run scan_deconflict "kanban-issue" '[{"number":1,"headRefName":"kanban-issue7-some-pr"}]'
    [ "$status" -eq 0 ]
    [[ "$output" == *"7"* ]]
}

@test "falls back to local-only when PR JSON is empty string" {
    export SCAN_MOCK_BRANCHES="  kanban-issue10-local-only"
    run scan_deconflict "kanban-issue" ""
    [ "$status" -eq 0 ]
    [[ "$output" == *"10"* ]]
}

@test "returns empty output when no matching branches or PRs" {
    export SCAN_MOCK_BRANCHES=""
    run scan_deconflict "kanban-issue" "[]"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "collects multiple distinct issue numbers" {
    export SCAN_MOCK_BRANCHES="  kanban-issue42-local
  kanban-issue99-other"
    run scan_deconflict "kanban-issue" '[{"number":1,"headRefName":"kanban-issue7-pr"}]'
    [ "$status" -eq 0 ]
    [[ "$output" == *"42"* ]]
    [[ "$output" == *"99"* ]]
    [[ "$output" == *"7"* ]]
}

@test "matches remote-tracking refs by stripping the remote prefix" {
    export SCAN_MOCK_BRANCHES="origin/kanban-issue42-remote-feature"
    run scan_deconflict "kanban-issue" ""
    [ "$status" -eq 0 ]
    [[ "$output" == *"42"* ]]
}
