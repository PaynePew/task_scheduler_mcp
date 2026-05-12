#!/usr/bin/env bash
# scan_deconflict BRANCH_PREFIX [PR_JSON]
# Outputs newline-separated issue numbers claimed by in-progress branches
# (local OR remote-tracking) or open PRs. Falls back to local-only if gh
# fails or PR_JSON arg is omitted. Silently skips malformed branch names.
#
# Branch naming convention: {prefix}{N}-{description}, e.g. kanban-issue42-foo
#
# Testing: set SCAN_MOCK_BRANCHES to a newline-delimited branch list to
# skip the `git branch` shell-out. Mock format mirrors
# `git branch -a --format='%(refname:short)'` (one ref per line, no leading
# "* " markers; remote-tracking refs may appear as "origin/<name>").

scan_deconflict() {
    local prefix="$1"
    local have_pr_arg="${2+set}"
    local pr_json="${2:-}"

    # ── Local + remote-tracking branches ──────────────────────────────────────
    local branches_raw
    if [[ -n "${SCAN_MOCK_BRANCHES+x}" ]]; then
        branches_raw="$SCAN_MOCK_BRANCHES"
    else
        branches_raw=$(git branch -a --format='%(refname:short)' 2>/dev/null || true)
    fi

    local branch
    while IFS= read -r branch; do
        # Tolerate legacy `git branch` formatting in mock data:
        # the "* " current-branch marker, and the 2-space indent prefix
        # that plain `git branch` (no --format) emits for non-current refs.
        branch="${branch#\* }"
        branch="${branch#"${branch%%[![:space:]]*}"}"
        # Strip remote-tracking prefix ("origin/kanban-issue42-foo" → "kanban-issue42-foo").
        local local_name="${branch#*/}"
        if [[ "$local_name" =~ ^${prefix}([0-9]+)- ]]; then
            echo "${BASH_REMATCH[1]}"
        fi
    done <<< "$branches_raw"

    # ── Open PRs ──────────────────────────────────────────────────────────────
    if [[ "$have_pr_arg" != "set" ]]; then
        pr_json=$(gh pr list --state open --json number,headRefName 2>/dev/null || true)
    fi

    if [[ -n "$pr_json" ]]; then
        local ref
        while IFS= read -r ref; do
            if [[ "$ref" =~ ^${prefix}([0-9]+)- ]]; then
                echo "${BASH_REMATCH[1]}"
            fi
        done < <(printf '%s' "$pr_json" | grep -o '"headRefName":"[^"]*"' | sed 's/"headRefName":"//;s/"//')
    fi
}
