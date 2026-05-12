#!/usr/bin/env bats

HARNESS_ROOT="$BATS_TEST_DIRNAME/.."
REPO_ROOT="$BATS_TEST_DIRNAME/../.."

@test "run-issue.ps1 is gone" {
    [ ! -f "$HARNESS_ROOT/run-issue.ps1" ]
}

@test "run-hello.ps1 is gone" {
    [ ! -f "$HARNESS_ROOT/run-hello.ps1" ]
}

@test "feature.md is gone" {
    [ ! -f "$HARNESS_ROOT/feature.md" ]
}

@test ".sandcastle directory is gone" {
    [ ! -d "$REPO_ROOT/.sandcastle" ]
}
