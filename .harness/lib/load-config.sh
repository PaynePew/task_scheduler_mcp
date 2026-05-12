#!/usr/bin/env bash
# load_config CONFIG_PATH
# Parses a YAML config (up to 3 levels), validates required keys, applies defaults.
# Exports: HARNESS_IMAGE, HARNESS_BRANCH_PREFIX, HARNESS_TRACKER_TYPE,
#          HARNESS_TRACKER_REPO, HARNESS_DEFAULT_MODEL,
#          HARNESS_AGENT_IMPLEMENT_MODEL, HARNESS_AGENT_IMPLEMENT_MAX_TURNS,
#          HARNESS_AGENT_PLAN_MODEL, HARNESS_AGENT_PLAN_MAX_TURNS,
#          HARNESS_DOCS_ADR_DIR (optional, may be empty)

# _yaml_top FILE KEY → prints scalar value of a top-level YAML key, or empty.
_yaml_top() {
    local file="$1" key="$2"
    awk -v k="$key" '
        $0 ~ "^" k ":" {
            sub("^" k ":[[:space:]]*", "")
            sub(/[[:space:]]+$/, "")
            print; exit
        }
    ' "$file"
}

# _yaml_nested FILE PARENT CHILD → prints scalar value of PARENT.CHILD, or empty.
_yaml_nested() {
    local file="$1" parent="$2" child="$3"
    awk -v p="$parent" -v c="$child" '
        $0 ~ "^" p ":"      { in_p = 1; next }
        in_p && /^[^ ]/     { in_p = 0 }
        in_p && $0 ~ "^  " c ":" {
            sub("^[[:space:]]*" c ":[[:space:]]*", "")
            sub(/[[:space:]]+$/, "")
            print; exit
        }
    ' "$file"
}

# _yaml_deep FILE GRANDPARENT PARENT CHILD → prints scalar of GRANDPARENT.PARENT.CHILD, or empty.
_yaml_deep() {
    local file="$1" gp="$2" p="$3" c="$4"
    awk -v gp="$gp" -v p="$p" -v c="$c" '
        $0 ~ "^" gp ":"          { in_gp = 1; in_p = 0; next }
        in_gp && /^[^ ]/         { in_gp = 0; in_p = 0 }
        in_gp && $0 ~ "^  " p ":" { in_p = 1; next }
        in_p && /^  [^ ]/        { in_p = 0 }
        in_p && $0 ~ "^    " c ":" {
            sub("^[[:space:]]*" c ":[[:space:]]*", "")
            sub(/[[:space:]]+$/, "")
            print; exit
        }
    ' "$file"
}

load_config() {
    local config_path="$1"

    if [[ ! -f "$config_path" ]]; then
        echo "ERROR: Config file not found: $config_path" >&2
        return 1
    fi

    # Match PS contract: reject tab indentation. Bash awk parsing would
    # silently mis-locate nested keys under mixed tab/space indents.
    if grep -q $'\t' "$config_path"; then
        echo "ERROR: Tab indentation found in $config_path. Use spaces only." >&2
        return 1
    fi

    HARNESS_IMAGE=$(_yaml_top "$config_path" "image")
    HARNESS_BRANCH_PREFIX=$(_yaml_top "$config_path" "branch_prefix")
    HARNESS_TRACKER_TYPE=$(_yaml_nested "$config_path" "tracker" "type")
    HARNESS_TRACKER_REPO=$(_yaml_nested "$config_path" "tracker" "repo")
    HARNESS_DEFAULT_MODEL=$(_yaml_nested "$config_path" "defaults" "model")
    HARNESS_AGENT_IMPLEMENT_MODEL=$(_yaml_deep "$config_path" "agents" "implement" "model")
    HARNESS_AGENT_IMPLEMENT_MAX_TURNS=$(_yaml_deep "$config_path" "agents" "implement" "max_turns")
    HARNESS_AGENT_PLAN_MODEL=$(_yaml_deep "$config_path" "agents" "plan" "model")
    HARNESS_AGENT_PLAN_MAX_TURNS=$(_yaml_deep "$config_path" "agents" "plan" "max_turns")
    HARNESS_DOCS_PRD_DIR=$(_yaml_nested "$config_path" "docs" "prd_dir")
    HARNESS_DOCS_CONTEXT=$(_yaml_nested "$config_path" "docs" "context")
    HARNESS_DOCS_ADR_DIR=$(_yaml_nested "$config_path" "docs" "adr_dir")
    # Flat-format only on bash. PS supports named-sub-entries with `when:` predicates.
    HARNESS_TESTS_BLOCK=$(_yaml_nested "$config_path" "tests" "block")
    HARNESS_TYPECHECK_BLOCK=$(_yaml_nested "$config_path" "typecheck" "block")
    HARNESS_COMMIT_STYLE=$(_yaml_nested "$config_path" "commit" "style")

    if [[ -z "$HARNESS_IMAGE" ]]; then
        echo "ERROR: Missing required config key 'image' in $config_path." >&2
        return 1
    fi
    if [[ -z "$HARNESS_BRANCH_PREFIX" ]]; then
        echo "ERROR: Missing required config key 'branch_prefix' in $config_path." >&2
        return 1
    fi
    if [[ -z "$HARNESS_TRACKER_TYPE" ]]; then
        echo "ERROR: Missing required config key 'tracker.type' in $config_path." >&2
        return 1
    fi
    if [[ "$HARNESS_TRACKER_TYPE" != "github" ]]; then
        echo "ERROR: tracker.type must be 'github' (v1 only supports github). Got: '$HARNESS_TRACKER_TYPE' in $config_path." >&2
        return 1
    fi
    if [[ -z "$HARNESS_TRACKER_REPO" ]]; then
        echo "ERROR: Missing required config key 'tracker.repo' in $config_path." >&2
        return 1
    fi

    HARNESS_DEFAULT_MODEL="${HARNESS_DEFAULT_MODEL:-claude-sonnet-4-6}"
    HARNESS_AGENT_IMPLEMENT_MODEL="${HARNESS_AGENT_IMPLEMENT_MODEL:-claude-sonnet-4-6}"
    HARNESS_AGENT_IMPLEMENT_MAX_TURNS="${HARNESS_AGENT_IMPLEMENT_MAX_TURNS:-80}"
    HARNESS_AGENT_PLAN_MODEL="${HARNESS_AGENT_PLAN_MODEL:-claude-opus-4-7}"
    HARNESS_AGENT_PLAN_MAX_TURNS="${HARNESS_AGENT_PLAN_MAX_TURNS:-10}"

    export HARNESS_IMAGE HARNESS_BRANCH_PREFIX HARNESS_TRACKER_TYPE HARNESS_TRACKER_REPO \
           HARNESS_DEFAULT_MODEL \
           HARNESS_AGENT_IMPLEMENT_MODEL HARNESS_AGENT_IMPLEMENT_MAX_TURNS \
           HARNESS_AGENT_PLAN_MODEL HARNESS_AGENT_PLAN_MAX_TURNS \
           HARNESS_DOCS_PRD_DIR HARNESS_DOCS_CONTEXT HARNESS_DOCS_ADR_DIR \
           HARNESS_TESTS_BLOCK HARNESS_TYPECHECK_BLOCK HARNESS_COMMIT_STYLE
}
