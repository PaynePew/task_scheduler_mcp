#!/usr/bin/env bash
# Demo hook: echoes HARNESS_* env vars to a file for test inspection.
echo "before-tests: issue=$HARNESS_ISSUE branch=$HARNESS_BRANCH phase=$HARNESS_PHASE" \
    >> "${HARNESS_HOOK_LOG:-/tmp/harness-hook.log}"
