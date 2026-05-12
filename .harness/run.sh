#!/usr/bin/env bash
# Generic Docker agent harness entry point for Linux / macOS / CI.
#
# Usage:
#   ./.harness/run.sh                  # plan → confirm → implement
#   ./.harness/run.sh --plan           # plan only, print ranking, no implement
#   ./.harness/run.sh --yes            # plan + auto-confirm + implement top candidate
#   ./.harness/run.sh --smoke-test
#   ./.harness/run.sh --issue 28
#   ./.harness/run.sh --issue 28 --resume   # resume after rate-limit / crash
set -euo pipefail

HARNESS_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HARNESS_ROOT/.." && pwd)"

# shellcheck source=lib/load-config.sh
source "$HARNESS_ROOT/lib/load-config.sh"
# shellcheck source=lib/render-prompt.sh
source "$HARNESS_ROOT/lib/render-prompt.sh"
# shellcheck source=lib/image-cache.sh
source "$HARNESS_ROOT/lib/image-cache.sh"
# shellcheck source=lib/heartbeat.sh
source "$HARNESS_ROOT/lib/heartbeat.sh"
# shellcheck source=lib/parse-plan.sh
source "$HARNESS_ROOT/lib/parse-plan.sh"
# shellcheck source=lib/scan-deconflict.sh
source "$HARNESS_ROOT/lib/scan-deconflict.sh"
# shellcheck source=lib/branch-claim.sh
source "$HARNESS_ROOT/lib/branch-claim.sh"

# ── Helpers ────────────────────────────────────────────────────────────────────

fail() {
    echo "ERROR: $1" >&2
    [[ -n "${2:-}" ]] && echo "  Run: $2" >&2
    exit 1
}

step() { printf '\e[36m── %s \e[90m%s\e[0m\n' "$1" "$(printf '%.0s─' {1..40})"; }

# ── Args ───────────────────────────────────────────────────────────────────────

SMOKE_TEST=false
PLAN_ONLY=false
AUTO_YES=false
RESUME=false
ISSUE_NUMBER=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --smoke-test) SMOKE_TEST=true ;;
        --plan)       PLAN_ONLY=true ;;
        --yes)        AUTO_YES=true ;;
        --resume)     RESUME=true ;;
        --issue)      shift; ISSUE_NUMBER="$1" ;;
        *) fail "Unknown argument: $1. Use --smoke-test, --plan, --yes, --resume, or --issue N." ;;
    esac
    shift
done

# ── Pre-flight checks ──────────────────────────────────────────────────────────

step 'Pre-flight checks'

# 1. CLAUDE_CODE_OAUTH_TOKEN
if [[ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]]; then
    ENV_FILE="$HARNESS_ROOT/.env.local"
    if [[ -f "$ENV_FILE" ]]; then
        # shellcheck disable=SC1090
        set -o allexport; source "$ENV_FILE"; set +o allexport
    fi
fi
[[ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]] && \
    fail "Missing CLAUDE_CODE_OAUTH_TOKEN." "claude setup-token"

# 2. Docker daemon
docker info >/dev/null 2>&1 || fail "Docker daemon not running. Start Docker and retry."

# 3. gh auth
gh auth status >/dev/null 2>&1 || fail "Not authenticated with GitHub CLI." "gh auth login"

# 4. git repo
[[ -d "$REPO_ROOT/.git" ]] || fail "Not inside a git repository."

echo "  All pre-flight checks passed."

# ── Load config ────────────────────────────────────────────────────────────────

step 'Loading config'
CONFIG_PATH="$HARNESS_ROOT/config.yml"
if [[ ! -f "$CONFIG_PATH" ]]; then
    EXAMPLE_PATH="$HARNESS_ROOT/config.yml.example"
    if [[ -f "$EXAMPLE_PATH" ]]; then
        fail "Missing $CONFIG_PATH." "cp $EXAMPLE_PATH $CONFIG_PATH   # then edit tracker.repo etc."
    else
        fail "Missing $CONFIG_PATH and no .example template found." "Create .harness/config.yml from scratch"
    fi
fi
load_config "$CONFIG_PATH"
IMAGE_NAME="$HARNESS_IMAGE"
MARKER_PATH="$HARNESS_ROOT/.image-hash"
echo "  image=$IMAGE_NAME  branch_prefix=$HARNESS_BRANCH_PREFIX"

# ── Image cache check / rebuild ────────────────────────────────────────────────

step 'Image cache check'
if [[ "$(image_rebuild_needed "$HARNESS_ROOT/Dockerfile" "$MARKER_PATH" "$IMAGE_NAME")" == "true" ]]; then
    echo "  Rebuilding image: $IMAGE_NAME"
    docker build -t "$IMAGE_NAME" -f "$HARNESS_ROOT/Dockerfile" "$REPO_ROOT"
    save_image_hash "$HARNESS_ROOT/Dockerfile" "$MARKER_PATH"
    echo "  Image built and hash cached."
else
    echo "  Image up-to-date — no rebuild needed."
fi

# ── Plan phase (bare, --plan, --yes) ──────────────────────────────────────────

if ! $SMOKE_TEST && [[ -z "$ISSUE_NUMBER" ]]; then
    step 'Plan phase'

    PLAN_MODEL="$HARNESS_AGENT_PLAN_MODEL"
    PLAN_MAX_TURNS="$HARNESS_AGENT_PLAN_MAX_TURNS"

    # Deconflict: collect in-progress issue numbers
    EXCL_LIST=$(scan_deconflict "$HARNESS_BRANCH_PREFIX" 2>/dev/null | sort -u | tr '\n' ',' | sed 's/,$//')
    IN_PROGRESS="${EXCL_LIST:-none}"
    echo "  In-progress: $IN_PROGRESS"

    ADR_DIR=""
    if [[ -n "${HARNESS_DOCS_ADR_DIR:-}" ]]; then
        ADR_DIR="$REPO_ROOT/$HARNESS_DOCS_ADR_DIR"
    fi
    ADR_NAMES=""
    if [[ -n "$ADR_DIR" && -d "$ADR_DIR" ]]; then
        ADR_NAMES=$(ls "$ADR_DIR"/*.md 2>/dev/null | xargs -n1 basename | tr '\n' ',' | sed 's/,$//')
    fi

    TRACKER_REPO="$HARNESS_TRACKER_REPO"
    LABEL_FLAG=""  # populated if tracker.filter_label is set in future

    PLAN_FILE="$HARNESS_ROOT/prompts/plan.md"
    [[ -f "$PLAN_FILE" ]] || fail "Plan prompt not found: $PLAN_FILE"

    RENDERED=$(render_prompt "$(cat "$PLAN_FILE")" \
        "REPO=$TRACKER_REPO" \
        "BRANCH_PREFIX=$HARNESS_BRANCH_PREFIX" \
        "IN_PROGRESS_LIST=$IN_PROGRESS" \
        "ADR_FILENAMES=$ADR_NAMES" \
        "TRACKER_LABEL_FLAG=$LABEL_FLAG")

    PROMPT_MOUNT="$HARNESS_ROOT/.current-prompt.md"
    printf '%s\n' "$RENDERED" > "$PROMPT_MOUNT"
    trap 'rm -f "$PROMPT_MOUNT"' EXIT

    LOG_FILE="$HARNESS_ROOT/logs/plan-$(date +%Y%m%d-%H%M%S).log"
    mkdir -p "$(dirname "$LOG_FILE")"

    printf '\e[36mIssue: ?  Agent: %s  Branch: (pending)  Log: %s\e[0m\n' "$PLAN_MODEL" "$LOG_FILE"
    echo "  max_turns=$PLAN_MAX_TURNS"

    export HB_TURNS=0 HB_ELAPSED_S=0 HB_LAST_ACTION=""
    LAST_HB=""

    claude_cmd="claude --output-format stream-json --verbose --model $PLAN_MODEL --max-turns $PLAN_MAX_TURNS -p \"\$(cat /workspace/.harness/.current-prompt.md)\""

    # parse_plan reads the full log file later (which embeds the <plan>
    # block inside the result event), so we don't accumulate content here.
    while IFS= read -r line; do
        echo "$line" >> "$LOG_FILE"
        if printf '%s' "$line" | grep -q '"type":"'; then
            heartbeat_reduce "$line"
            hb_line="  [turns=$HB_TURNS elapsed=${HB_ELAPSED_S}s action=$HB_LAST_ACTION]"
            if [[ -n "$LAST_HB" ]]; then printf '\e[1A\e[2K'; fi
            echo "$hb_line"
            LAST_HB="$hb_line"
        fi
    done < <(docker run --rm \
        --volume "${REPO_ROOT}:/workspace" \
        --env    CLAUDE_CODE_OAUTH_TOKEN \
        --workdir /workspace \
        "$IMAGE_NAME" \
        bash -lc "$claude_cmd" 2>&1)

    PLAN_EXIT="${PIPESTATUS[0]:-0}"

    if [[ -n "$LAST_HB" ]]; then printf '\e[1A\e[2K'; fi
    echo "  Plan agent complete."

    if [[ "${PLAN_EXIT}" -ne 0 ]]; then
        echo "ERROR: Plan agent failed (exit ${PLAN_EXIT})." >&2
        exit "${PLAN_EXIT}"
    fi

    # Parse <plan> block from accumulated log content
    PLAN_JSON=$(parse_plan "$(cat "$LOG_FILE")" 2>/dev/null) || {
        echo "ERROR: Could not parse plan output. Raw log: $LOG_FILE" >&2
        exit 1
    }

    # Extract top.* via parse-plan helpers — robust to JSON key ordering.
    TOP_ID=$(parse_plan_top_id    "$PLAN_JSON") || {
        echo "ERROR: Could not extract top.id from plan JSON. Raw log: $LOG_FILE" >&2
        exit 1
    }
    TOP_TITLE=$(parse_plan_top_field  title  "$PLAN_JSON")
    TOP_BRANCH=$(parse_plan_top_field branch "$PLAN_JSON")

    printf '\n\e[36m┌─ Plan ranking ─────────────────────────────────────────────┐\e[0m\n'
    printf '\e[32m│  TOP  #%s — %s\e[0m\n' "$TOP_ID" "$TOP_TITLE"
    printf '\e[90m│       Branch: %s\e[0m\n' "$TOP_BRANCH"
    printf '\e[36m└────────────────────────────────────────────────────────────┘\e[0m\n\n'

    $PLAN_ONLY && exit 0

    if $AUTO_YES; then
        echo "  Auto-confirming #$TOP_ID ($TOP_TITLE)..."
        CONFIRMED=true
    else
        printf 'Run #%s — %s? [Y/n] ' "$TOP_ID" "$TOP_TITLE"
        read -r ans
        case "${ans:-Y}" in [Yy]*) CONFIRMED=true ;; *) CONFIRMED=false ;; esac
    fi

    if ! $CONFIRMED; then
        echo "  Exiting — no branch created."
        exit 0
    fi

    echo "  Selected #$TOP_ID — chaining into implement phase..."
    exec "$HARNESS_ROOT/run.sh" --issue "$TOP_ID"
fi

# ── Select and render prompt (smoke-test / implement) ─────────────────────────

BRANCH_NAME=""

if $SMOKE_TEST; then
    PROMPT_FILE="$HARNESS_ROOT/prompts/smoke-test.md"
    LOG_FILE="$HARNESS_ROOT/logs/smoke-test.log"
    RUN_LABEL="smoke-test"
    RENDERED=$(render_prompt "$(cat "$PROMPT_FILE")")
else
    # Derive kebab slug from issue title, then atomically claim the branch.
    step 'Claiming branch'
    ISSUE_TITLE=$(gh issue view "$ISSUE_NUMBER" --repo "$HARNESS_TRACKER_REPO" --json title --jq '.title' 2>&1) \
        || fail "gh issue view #$ISSUE_NUMBER failed: $ISSUE_TITLE"
    SLUG=$(printf '%s' "$ISSUE_TITLE" | tr '[:upper:]' '[:lower:]' \
        | sed -E 's/[^a-z0-9]+/-/g; s/^-+//; s/-+$//' | cut -c1-40 | sed -E 's/-+$//')
    [[ -n "$SLUG" ]] || fail "Could not derive slug from issue #$ISSUE_NUMBER title: '$ISSUE_TITLE'"

    if ! BRANCH_NAME=$(invoke_branch_claim "$HARNESS_BRANCH_PREFIX" "$ISSUE_NUMBER" "$SLUG" "$RESUME"); then
        fail "Branch claim failed for issue #$ISSUE_NUMBER (see error above)" "./.harness/run.sh --issue $ISSUE_NUMBER --resume"
    fi
    echo "  Branch: $BRANCH_NAME"

    # Target branch (default branch of the repo) — used by implement/review/merge
    # prompts so non-`main` repos work. Fall back to "main" if HEAD is unset.
    TARGET_BRANCH=$(git symbolic-ref refs/remotes/origin/HEAD --short 2>/dev/null || echo origin/main)
    TARGET_BRANCH="${TARGET_BRANCH#origin/}"

    PROMPT_FILE="$HARNESS_ROOT/prompts/implement.md"
    LOG_FILE="$HARNESS_ROOT/logs/issue-${ISSUE_NUMBER}.log"
    RUN_LABEL="issue-${ISSUE_NUMBER}"
    RENDERED=$(render_prompt "$(cat "$PROMPT_FILE")" \
        "ISSUE=$ISSUE_NUMBER" \
        "BRANCH=$BRANCH_NAME" \
        "TARGET_BRANCH=$TARGET_BRANCH" \
        "DOCS_PRD_DIR=${HARNESS_DOCS_PRD_DIR:-}" \
        "DOCS_CONTEXT=${HARNESS_DOCS_CONTEXT:-}" \
        "DOCS_ADR_DIR=${HARNESS_DOCS_ADR_DIR:-}" \
        "TESTS_BLOCK=${HARNESS_TESTS_BLOCK:-}" \
        "TYPECHECK_BLOCK=${HARNESS_TYPECHECK_BLOCK:-}" \
        "COMMIT_STYLE=${HARNESS_COMMIT_STYLE:-}")
fi

[[ -f "$PROMPT_FILE" ]] || fail "Prompt file not found: $PROMPT_FILE"

PROMPT_MOUNT="$HARNESS_ROOT/.current-prompt.md"
printf '%s\n' "$RENDERED" > "$PROMPT_MOUNT"
trap 'rm -f "$PROMPT_MOUNT"' EXIT

# ── Run container ──────────────────────────────────────────────────────────────

step "Running $RUN_LABEL"
echo "  Log → $LOG_FILE"
mkdir -p "$(dirname "$LOG_FILE")"

# Pass the token by reference (no `=value`) so it doesn't appear in
# the host process listing. Docker reads it from our environment.
if $SMOKE_TEST; then
    CLAUDE_CMD='claude --permission-mode bypassPermissions -p "$(cat /workspace/.harness/.current-prompt.md)"'
else
    CLAUDE_CMD="claude --permission-mode bypassPermissions --model $HARNESS_AGENT_IMPLEMENT_MODEL --max-turns $HARNESS_AGENT_IMPLEMENT_MAX_TURNS -p \"\$(cat /workspace/.harness/.current-prompt.md)\""
fi

docker run --rm \
    --volume "${REPO_ROOT}:/workspace" \
    --env    CLAUDE_CODE_OAUTH_TOKEN \
    --env    GH_TOKEN \
    --workdir /workspace \
    "$IMAGE_NAME" \
    bash -lc "$CLAUDE_CMD" \
    2>&1 | tee "$LOG_FILE"

EXIT_CODE="${PIPESTATUS[0]}"

# ── Summary ────────────────────────────────────────────────────────────────────

if [[ "$EXIT_CODE" -eq 0 ]]; then
    STATUS="COMPLETE"
    COLOR='\e[32m'
else
    STATUS="FAILED (exit $EXIT_CODE)"
    COLOR='\e[31m'
fi

printf '\n'
printf "${COLOR}╔══════════════════════════════════════════════════╗\e[0m\n"
printf "${COLOR}║  %-46s  ║\e[0m\n" "$RUN_LABEL — $STATUS"
printf "${COLOR}╚══════════════════════════════════════════════════╝\e[0m\n"

[[ "$EXIT_CODE" -eq 0 && "$SMOKE_TEST" == "true" ]] && \
    echo "  Log saved to: $LOG_FILE"

exit "$EXIT_CODE"
