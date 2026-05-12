#!/usr/bin/env bash
# parse_plan CONTENT
# Extracts the last <plan>...</plan> block from content, validates required keys.
# On success: prints the plan JSON to stdout, returns 0.
# On failure: prints ERROR message to stderr, returns 1.

parse_plan() {
    local content="$1"

    # Extract all <plan>...</plan> blocks (including multiline); keep only the last.
    local last_plan=""
    local in_plan=0
    local buf=""

    while IFS= read -r line; do
        if [[ "$line" == *"<plan>"* ]]; then
            in_plan=1
            buf=""
            # Capture content after the opening tag on the same line
            local after_tag="${line#*<plan>}"
            after_tag="${after_tag%</plan>*}"
            buf="$after_tag"
            # If closing tag is on the same line, close immediately
            if [[ "$line" == *"</plan>"* ]]; then
                last_plan="$buf"
                in_plan=0
            fi
        elif [[ "$in_plan" -eq 1 ]]; then
            if [[ "$line" == *"</plan>"* ]]; then
                buf="${buf}${line%</plan>*}"
                last_plan="$buf"
                in_plan=0
            else
                buf="${buf}${line}"
            fi
        fi
    done <<< "$content"

    if [[ -z "$last_plan" ]]; then
        echo "ERROR: No <plan> block found in content." >&2
        return 1
    fi

    # Trim whitespace
    local plan_json
    plan_json=$(printf '%s' "$last_plan" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')

    # Validate required keys are present (grep-based; no jq dependency)
    for key in top alternatives blocked; do
        if ! printf '%s' "$plan_json" | grep -q "\"$key\""; then
            echo "ERROR: Missing required key '$key' in plan JSON." >&2
            return 1
        fi
    done

    printf '%s\n' "$plan_json"
}

# parse_plan_top_id PLAN_JSON
# Extracts top.id from plan JSON, robust to key ordering (alternatives may
# appear before top). Schema guarantees top has only scalar fields, so the
# inner braced block "{...}" is matched non-greedily up to the first '}'.
# On success: prints the integer id, returns 0.
# On failure: returns 1 (no <plan>.top found).
parse_plan_top_id() {
    local plan_json="$1"
    local top_block
    top_block=$(printf '%s' "$plan_json" | sed -n 's/.*"top":[[:space:]]*\({[^}]*}\).*/\1/p')
    if [[ -z "$top_block" ]]; then
        return 1
    fi
    local id
    id=$(printf '%s' "$top_block" | grep -o '"id":[0-9]*' | head -1 | sed 's/"id"://')
    if [[ -z "$id" ]]; then
        return 1
    fi
    printf '%s' "$id"
}

# parse_plan_top_field FIELD PLAN_JSON
# Extracts a string scalar field (title, branch, reason) from plan.top.
parse_plan_top_field() {
    local field="$1"
    local plan_json="$2"
    local top_block
    top_block=$(printf '%s' "$plan_json" | sed -n 's/.*"top":[[:space:]]*\({[^}]*}\).*/\1/p')
    if [[ -z "$top_block" ]]; then
        return 1
    fi
    printf '%s' "$top_block" | grep -o "\"$field\":\"[^\"]*\"" | head -1 | sed "s/\"$field\":\"//;s/\"\$//"
}
