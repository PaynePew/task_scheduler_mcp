"""setup_summary MCP prompt — template for creating a scheduled task."""

from __future__ import annotations

import mcp.types as types
from mcp.shared.exceptions import McpError
from mcp.types import INVALID_PARAMS, ErrorData

NAME = "setup_summary"
DESCRIPTION = (
    "Set up a new scheduled task: consult the available actions registry and "
    "create a task for the given topic on the requested schedule."
)

_TEMPLATE = (
    "I want to set up a scheduled task about '{topic}' to run '{schedule}'. "
    "First, read the tasks://actions resource to discover the available actions "
    "and their parameter schemas. Then call task.create.v1 with an appropriate "
    "action, cron_expr, and action_params that match the topic and schedule I described."
)


def build_prompt() -> types.Prompt:
    return types.Prompt(
        name=NAME,
        description=DESCRIPTION,
        arguments=[
            types.PromptArgument(
                name="topic",
                description=(
                    "The subject or purpose of the task (e.g. 'AI news', 'daily standup reminder')."
                ),
                required=True,
            ),
            types.PromptArgument(
                name="schedule",
                description=(
                    "When the task should run, in natural language "
                    "(e.g. 'every morning at 8am', 'weekdays at noon')."
                ),
                required=True,
            ),
        ],
    )


def build_result(topic: str, schedule: str) -> types.GetPromptResult:
    text = _TEMPLATE.format(topic=topic, schedule=schedule)
    return types.GetPromptResult(
        description=DESCRIPTION,
        messages=[
            types.PromptMessage(
                role="user",
                content=types.TextContent(type="text", text=text),
            )
        ],
    )


def validate_args(arguments: dict[str, str] | None) -> tuple[str, str]:
    """Extract and validate topic + schedule from arguments dict.

    Raises McpError(INVALID_PARAMS) if either required arg is absent.
    Empty strings are allowed (stringly-typed, content validation is LLM-side).
    """
    args = arguments or {}
    missing = [f for f in ("topic", "schedule") if f not in args]
    if missing:
        raise McpError(
            ErrorData(
                code=INVALID_PARAMS,
                message=f"Missing required argument(s): {', '.join(missing)}",
            )
        )
    return args["topic"], args["schedule"]
