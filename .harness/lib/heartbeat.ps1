#Requires -Version 7
# Pure reducer: reduce(state, stream_json_event) → new_state.
# No I/O, no side effects.
#
# Operates on Claude Code's real stream-json shape:
#   {"type":"system","subtype":"init","model":"..."}
#   {"type":"assistant","message":{"content":[{"type":"text","text":"..."},
#                                             {"type":"tool_use","name":"..","input":{..}}]}}
#   {"type":"user","message":{"content":[{"type":"tool_result", ...}]}}
#   {"type":"result","num_turns":N,"duration_ms":M,"result":"..."}

# Pure renderer: state + (optional wall-clock) → single heartbeat line.
# When -StartTime is supplied, elapsed is computed from wall-clock so it
# advances during the run instead of staying at 0 until the result event.
function Format-HeartbeatLine {
    param(
        [Parameter(Mandatory)][hashtable]$State,
        [datetime]$StartTime,
        [datetime]$Now
    )
    $elapsed = if ($PSBoundParameters.ContainsKey('StartTime')) {
        if (-not $PSBoundParameters.ContainsKey('Now')) { $Now = Get-Date }
        [int][Math]::Floor(($Now - $StartTime).TotalSeconds)
    } else {
        $State.elapsed_s
    }
    return "  [turns=$($State.turns) elapsed=${elapsed}s action=$($State.last_action)]"
}

function Invoke-HeartbeatReduce {
    param(
        [Parameter(Mandatory)][hashtable]$State,
        [Parameter(Mandatory)][hashtable]$Event
    )

    $new = @{
        turns       = $State.turns
        elapsed_s   = $State.elapsed_s
        last_action = $State.last_action
    }

    switch ($Event.type) {
        'system' {
            if ($Event.subtype -eq 'init') {
                $new.turns       = 0
                $new.elapsed_s   = 0
                $new.last_action = 'init'
            }
        }
        'assistant' {
            $contents = @()
            if ($Event.ContainsKey('message') -and $Event.message -is [hashtable] -and $Event.message.ContainsKey('content')) {
                $contents = @($Event.message.content)
            }
            foreach ($item in $contents) {
                if ($item -isnot [hashtable]) { continue }
                switch ($item.type) {
                    'text' {
                        $new.turns       = $new.turns + 1
                        $new.last_action = 'thinking'
                    }
                    'tool_use' {
                        $toolName = if ($item.ContainsKey('name') -and $item.name) { $item.name } else { 'tool' }
                        $new.last_action = "tool:$toolName"
                    }
                }
            }
        }
        'result' {
            if ($Event.ContainsKey('duration_ms') -and $null -ne $Event.duration_ms) {
                $new.elapsed_s = [int][Math]::Floor([double]$Event.duration_ms / 1000)
            }
            if ($Event.ContainsKey('num_turns') -and $null -ne $Event.num_turns) {
                $new.turns = [int]$Event.num_turns
            }
            $new.last_action = 'done'
        }
        default {
            # Unknown event — pass through unchanged.
        }
    }

    return $new
}
