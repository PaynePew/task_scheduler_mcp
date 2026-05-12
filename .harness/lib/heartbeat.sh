#!/usr/bin/env bash
# heartbeat_reduce EVENT_JSON
# Pure reducer operating on env-var state: HB_TURNS, HB_ELAPSED_S, HB_LAST_ACTION.
# Unknown events leave state unchanged. No I/O side effects.
#
# Operates on Claude Code's real stream-json shape:
#   {"type":"system","subtype":"init","model":"..."}
#   {"type":"assistant","message":{"content":[{"type":"text","text":"..."},
#                                             {"type":"tool_use","name":"..","input":{..}}]}}
#   {"type":"user","message":{"content":[{"type":"tool_result", ...}]}}
#   {"type":"result","num_turns":N,"duration_ms":M,"result":"..."}
#
# No jq dependency — uses grep/sed to read top-level keys and nested content
# discriminators on a single-line event string.

heartbeat_reduce() {
    local event_json="$1"

    # Top-level event type (system / assistant / user / result).
    local event_type
    event_type=$(printf '%s' "$event_json" | grep -o '"type":"[a-z_]*"' | head -1 | sed 's/"type":"//;s/"$//')

    case "$event_type" in
        system)
            local subtype
            subtype=$(printf '%s' "$event_json" | grep -o '"subtype":"[a-z_]*"' | head -1 | sed 's/"subtype":"//;s/"$//')
            if [[ "$subtype" == "init" ]]; then
                HB_TURNS=0
                HB_ELAPSED_S=0
                HB_LAST_ACTION="init"
            fi
            ;;
        assistant)
            # Each text item in content[] counts as one turn; the last
            # tool_use (if any) overrides the action label.
            if printf '%s' "$event_json" | grep -q '"type":"text"'; then
                HB_TURNS=$(( ${HB_TURNS:-0} + 1 ))
                HB_LAST_ACTION="thinking"
            fi
            if printf '%s' "$event_json" | grep -q '"type":"tool_use"'; then
                local tool_name
                tool_name=$(printf '%s' "$event_json" | grep -o '"name":"[^"]*"' | head -1 | sed 's/"name":"//;s/"$//')
                HB_LAST_ACTION="tool:${tool_name:-tool}"
            fi
            ;;
        result)
            local duration_ms
            duration_ms=$(printf '%s' "$event_json" | grep -o '"duration_ms":[0-9]*' | head -1 | sed 's/"duration_ms"://')
            if [[ -n "$duration_ms" ]]; then
                HB_ELAPSED_S=$(( duration_ms / 1000 ))
            fi
            local num_turns
            num_turns=$(printf '%s' "$event_json" | grep -o '"num_turns":[0-9]*' | head -1 | sed 's/"num_turns"://')
            if [[ -n "$num_turns" ]]; then
                HB_TURNS="$num_turns"
            fi
            HB_LAST_ACTION="done"
            ;;
        *)
            # Unknown event — pass through unchanged.
            ;;
    esac

    export HB_TURNS HB_ELAPSED_S HB_LAST_ACTION
}
