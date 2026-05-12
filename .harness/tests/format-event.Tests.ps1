BeforeAll {
    . "$PSScriptRoot/../lib/format-event.ps1"
}

Describe 'Format-StreamEvent — system/init' {
    It 'extracts model and cwd into [INIT] line' {
        $event = @{
            type = 'system'
            subtype = 'init'
            model = 'claude-sonnet-4-6'
            cwd = '/workspace'
            claude_code_version = '2.1.138'
            permissionMode = 'default'
        }
        $out = Format-StreamEvent -Event $event
        $out | Should -Match '\[INIT\]'
        $out | Should -Match 'claude-sonnet-4-6'
        $out | Should -Match '/workspace'
    }

    It 'includes version and permissionMode on second line' {
        $event = @{
            type = 'system'; subtype = 'init'; model = 'm'; cwd = '/'
            claude_code_version = '2.1.138'; permissionMode = 'default'
        }
        $out = Format-StreamEvent -Event $event
        $out | Should -Match '2.1.138'
        $out | Should -Match 'permissionMode=default'
    }
}

Describe 'Format-StreamEvent — rate_limit_event' {
    It 'flags warning utilization with [WARN RATE] prefix' {
        $event = @{
            type = 'rate_limit_event'
            rate_limit_info = @{
                status = 'allowed_warning'
                rateLimitType = 'seven_day'
                utilization = 0.82
                surpassedThreshold = 0.75
                resetsAt = 1778965200
            }
        }
        $out = Format-StreamEvent -Event $event
        $out | Should -Match '\[. RATE\]|\[WARN RATE\]|\[RATE\]'
        $out | Should -Match '82%'
        $out | Should -Match 'seven.?day|7.day'
    }

    It 'includes resetsAt as a UTC timestamp' {
        $event = @{
            type = 'rate_limit_event'
            rate_limit_info = @{
                status = 'allowed_warning'; rateLimitType = 'seven_day'
                utilization = 0.82; surpassedThreshold = 0.75
                resetsAt = 1778965200
            }
        }
        $out = Format-StreamEvent -Event $event
        $out | Should -Match '2026-05-1[56]'
    }
}

Describe 'Format-StreamEvent — assistant text' {
    It 'extracts text content into [ASSIST] line' {
        $event = @{
            type = 'assistant'
            message = @{
                content = @(
                    @{ type = 'text'; text = 'PONG' }
                )
                usage = @{ output_tokens = 6 }
            }
        }
        $out = Format-StreamEvent -Event $event
        $out | Should -Match '\[ASSIST\]'
        $out | Should -Match 'PONG'
    }

    It 'reports tool_use as tool name' {
        $event = @{
            type = 'assistant'
            message = @{
                content = @(
                    @{ type = 'tool_use'; name = 'Bash'; input = @{ command = 'ls' } }
                )
            }
        }
        $out = Format-StreamEvent -Event $event
        $out | Should -Match '\[ASSIST\]'
        $out | Should -Match 'tool:Bash|Bash'
    }
}

Describe 'Format-StreamEvent — result' {
    It 'reports success with turns/duration/cost' {
        $event = @{
            type = 'result'
            subtype = 'success'
            is_error = $false
            num_turns = 1
            duration_ms = 2025
            total_cost_usd = 0.005976499999999999
            usage = @{ input_tokens = 2; output_tokens = 6; cache_read_input_tokens = 18205 }
        }
        $out = Format-StreamEvent -Event $event
        $out | Should -Match '\[RESULT\]'
        $out | Should -Match 'success|✓'
        $out | Should -Match '1 turn'
        $out | Should -Match '2\.0s|2025'
        $out | Should -Match '\$0\.006|\$0\.0060'
    }

    It 'reports failure with error indicator' {
        $event = @{
            type = 'result'
            subtype = 'error_max_turns'
            is_error = $true
            num_turns = 80
            duration_ms = 600000
            total_cost_usd = 1.234
            usage = @{ input_tokens = 100; output_tokens = 200; cache_read_input_tokens = 0 }
        }
        $out = Format-StreamEvent -Event $event
        $out | Should -Match '\[RESULT\]'
        $out | Should -Match 'fail|error|✗|FAIL'
    }
}

Describe 'Format-StreamEvent — unknown event' {
    It 'returns null for unrecognized event types' {
        $event = @{ type = 'something_new'; data = 'whatever' }
        $out = Format-StreamEvent -Event $event
        $out | Should -BeNullOrEmpty
    }
}
