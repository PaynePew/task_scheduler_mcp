#!/usr/bin/env bats

setup() {
    source "$BATS_TEST_DIRNAME/../lib/image-cache.sh"
    DF=$(mktemp)
    MARKER="${DF}.hash"
    echo 'FROM node:22' > "$DF"
}

teardown() {
    rm -f "$DF" "$MARKER"
}

@test "returns true when marker does not exist" {
    result=$(image_rebuild_needed "$DF" "$MARKER")
    [ "$result" = "true" ]
}

@test "returns false when hash matches" {
    save_image_hash "$DF" "$MARKER"
    result=$(image_rebuild_needed "$DF" "$MARKER")
    [ "$result" = "false" ]
}

@test "returns true when Dockerfile has changed" {
    save_image_hash "$DF" "$MARKER"
    echo 'RUN echo changed' >> "$DF"
    result=$(image_rebuild_needed "$DF" "$MARKER")
    [ "$result" = "true" ]
}

@test "returns true when marker is empty (corrupted)" {
    touch "$MARKER"
    result=$(image_rebuild_needed "$DF" "$MARKER")
    [ "$result" = "true" ]
}
