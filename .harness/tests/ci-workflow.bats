#!/usr/bin/env bats

WORKFLOW="$BATS_TEST_DIRNAME/../../.github/workflows/agent-harness.yml"

@test "workflow file exists" {
    [ -f "$WORKFLOW" ]
}

@test "triggers on pull_request for .harness paths" {
    grep -q "pull_request" "$WORKFLOW"
    grep -q "\.harness/" "$WORKFLOW"
}

@test "has a windows-latest runner job" {
    grep -q "windows-latest" "$WORKFLOW"
}

@test "has an ubuntu-latest runner job" {
    grep -q "ubuntu-latest" "$WORKFLOW"
}

@test "runs Pester on the windows job" {
    grep -qE "Pester|Invoke-Pester" "$WORKFLOW"
}

@test "runs bats on the ubuntu job" {
    grep -q "bats" "$WORKFLOW"
}
