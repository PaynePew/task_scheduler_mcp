#!/usr/bin/env bash
# invoke_branch_claim PREFIX ISSUE_NUMBER SLUG RESUME
# Mirrors lib/branch-claim.ps1's Invoke-BranchClaim contract:
#   - If a branch matching "{PREFIX}{N}-*" already exists locally:
#       RESUME=false → exit 1 with claimed-by-another-terminal message
#       RESUME=true  → checkout the existing branch, print its name
#   - Else if RESUME=true → exit 1 with "no matching branch to resume"
#   - Else                → git checkout -b "{PREFIX}{N}-{SLUG}", print name
#
# Tests can override the git helpers via these env vars (see tests/branch-claim.bats):
#   BRANCH_CLAIM_LIST_CMD     — command that lists branches (default: git branch ...)
#   BRANCH_CLAIM_CREATE_CMD   — command that creates+checks-out (default: git checkout -b)
#   BRANCH_CLAIM_CHECKOUT_CMD — command that checks out an existing branch (default: git checkout)
invoke_branch_claim() {
    local prefix="$1" issue="$2" slug="$3" resume="${4:-false}"
    local branch_name="${prefix}${issue}-${slug}"
    local pattern="${prefix}${issue}-"

    local list_cmd="${BRANCH_CLAIM_LIST_CMD:-git branch -a --format=%(refname:short)}"
    local create_cmd="${BRANCH_CLAIM_CREATE_CMD:-git checkout -b}"
    local checkout_cmd="${BRANCH_CLAIM_CHECKOUT_CMD:-git checkout}"

    local existing=""
    while IFS= read -r line; do
        line="${line## }"
        line="${line%% }"
        # Strip remote-tracking prefix so "origin/foo" matches "foo".
        local short="${line#origin/}"
        case "$short" in
            "${pattern}"*) existing="$short"; break ;;
        esac
    done < <($list_cmd 2>/dev/null)

    if [[ -n "$existing" ]]; then
        if [[ "$resume" != "true" ]]; then
            echo "ERROR: Branch already claimed by another terminal. Re-run with --resume to continue." >&2
            return 1
        fi
        $checkout_cmd "$existing" >/dev/null
        printf '%s\n' "$existing"
        return 0
    fi

    if [[ "$resume" == "true" ]]; then
        echo "ERROR: No matching branch found for pattern '${pattern}*'. Cannot resume." >&2
        return 1
    fi

    $create_cmd "$branch_name" >/dev/null
    printf '%s\n' "$branch_name"
}
