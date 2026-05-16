"""daily_review MCP prompt — template for reviewing scheduled tasks."""

from __future__ import annotations

import mcp.types as types

NAME = "daily_review"
DESCRIPTION = (
    "Review your scheduled tasks: list each task's status and next run time, "
    "and highlight any failed runs in the last 24 hours."
)

_TEMPLATE = (
    "Please review my scheduled tasks by reading the tasks://list resource. "
    "For each task, report its status and next scheduled run time. "
    "Highlight any tasks that had failed runs in the last 24 hours."
)


def build_prompt() -> types.Prompt:
    return types.Prompt(
        name=NAME,
        description=DESCRIPTION,
        arguments=[],
    )


def build_result() -> types.GetPromptResult:
    return types.GetPromptResult(
        description=DESCRIPTION,
        messages=[
            types.PromptMessage(
                role="user",
                content=types.TextContent(type="text", text=_TEMPLATE),
            )
        ],
    )
