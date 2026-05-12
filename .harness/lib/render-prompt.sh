#!/usr/bin/env bash
# render_prompt TEMPLATE [KEY=VALUE ...]
# Substitutes {{KEY}} placeholders. A line that contains *only* a placeholder
# (e.g. "  {{TESTS_BLOCK}}") is dropped when the value is empty; genuine blank
# lines and mixed-content lines are always preserved. Matches the PS contract.
render_prompt() {
    local template="$1"
    shift || true

    local placeholder_only_re='^[[:space:]]*\{\{[A-Z_0-9]+\}\}[[:space:]]*$'
    local output=""

    while IFS= read -r line; do
        local was_placeholder_only=0
        if [[ "$line" =~ $placeholder_only_re ]]; then
            was_placeholder_only=1
        fi

        # Literal substitution (parameter expansion ≠ regex; safe for $1, $&, etc.).
        for kv in "$@"; do
            local key="${kv%%=*}"
            local val="${kv#*=}"
            line="${line//\{\{$key\}\}/$val}"
        done

        # Strip any remaining {{KEY}} placeholders (unmapped keys → empty).
        line=$(printf '%s' "$line" | sed 's/{{[A-Z_0-9]*}}//g')

        # Drop only when originally placeholder-only AND now whitespace-only.
        if [[ "$was_placeholder_only" -eq 1 ]] && [[ ! "$line" =~ [^[:space:]] ]]; then
            continue
        fi
        output+="${line}"$'\n'
    done <<< "$template"

    printf '%s' "${output%$'\n'}"
}
