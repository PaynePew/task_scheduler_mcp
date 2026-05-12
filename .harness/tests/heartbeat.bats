#!/usr/bin/env bats

setup() {
    source "$BATS_TEST_DIRNAME/../lib/heartbeat.sh"
    export HB_TURNS=0
    export HB_ELAPSED_S=0
    export HB_LAST_ACTION=""
}

@test "system init resets state" {
    export HB_TURNS=5 HB_ELAPSED_S=10 HB_LAST_ACTION="tool:Bash"
    heartbeat_reduce '{"type":"system","subtype":"init","model":"claude-sonnet-4-6"}'
    [ "$HB_TURNS" -eq 0 ]
    [ "$HB_ELAPSED_S" = "0" ]
    [ "$HB_LAST_ACTION" = "init" ]
}

@test "system with non-init subtype is ignored" {
    export HB_TURNS=2 HB_LAST_ACTION="thinking"
    heartbeat_reduce '{"type":"system","subtype":"compact"}'
    [ "$HB_TURNS" -eq 2 ]
    [ "$HB_LAST_ACTION" = "thinking" ]
}

@test "assistant text increments turns and sets last_action to thinking" {
    export HB_TURNS=2
    heartbeat_reduce '{"type":"assistant","message":{"content":[{"type":"text","text":"hello"}]}}'
    [ "$HB_TURNS" -eq 3 ]
    [ "$HB_LAST_ACTION" = "thinking" ]
}

@test "assistant tool_use sets last_action with tool name" {
    heartbeat_reduce '{"type":"assistant","message":{"content":[{"type":"tool_use","id":"x","name":"Bash","input":{}}]}}'
    [ "$HB_LAST_ACTION" = "tool:Bash" ]
}

@test "assistant tool_use falls back to tool:tool when name is absent" {
    heartbeat_reduce '{"type":"assistant","message":{"content":[{"type":"tool_use","input":{}}]}}'
    [ "$HB_LAST_ACTION" = "tool:tool" ]
}

@test "assistant event with both text and tool_use lets tool_use win" {
    heartbeat_reduce '{"type":"assistant","message":{"content":[{"type":"text","text":"reading"},{"type":"tool_use","name":"Read","input":{}}]}}'
    [ "$HB_TURNS" -eq 1 ]
    [ "$HB_LAST_ACTION" = "tool:Read" ]
}

@test "result derives elapsed_s as floor(duration_ms / 1000)" {
    heartbeat_reduce '{"type":"result","duration_ms":42500,"num_turns":8,"result":"done"}'
    [ "$HB_ELAPSED_S" = "42" ]
}

@test "result replaces turns with authoritative num_turns" {
    heartbeat_reduce '{"type":"result","duration_ms":1000,"num_turns":8}'
    [ "$HB_TURNS" = "8" ]
}

@test "result sets last_action to done" {
    heartbeat_reduce '{"type":"result","duration_ms":10000,"num_turns":4}'
    [ "$HB_LAST_ACTION" = "done" ]
}

@test "user (tool_result) leaves state unchanged" {
    export HB_TURNS=4 HB_ELAPSED_S=5 HB_LAST_ACTION="tool:Bash"
    heartbeat_reduce '{"type":"user","message":{"content":[{"type":"tool_result","content":"ok"}]}}'
    [ "$HB_TURNS" -eq 4 ]
    [ "$HB_LAST_ACTION" = "tool:Bash" ]
}

@test "unknown event leaves state unchanged" {
    export HB_TURNS=2 HB_ELAPSED_S=5 HB_LAST_ACTION="thinking"
    heartbeat_reduce '{"type":"future.unrecognised","data":"x"}'
    [ "$HB_TURNS" -eq 2 ]
    [ "$HB_ELAPSED_S" = "5" ]
    [ "$HB_LAST_ACTION" = "thinking" ]
}
