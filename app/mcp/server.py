"""Transport-agnostic MCP server: task.create@v1 + task.list_actions@v1 per ADR-006/014."""

from __future__ import annotations

import json
import logging
from typing import Any

import mcp.types as types
from mcp.server.lowlevel import Server

from app.actions.registry import ACTION_REGISTRY
from app.db.engine import async_session_factory
from app.domain.jobs import create_job
from app.mcp.envelope import error, success
from app.mcp.errors import map_domain_error
from app.mcp.handlers.status import _TASK_STATUS_SCHEMA, handle_task_status

logger = logging.getLogger(__name__)

SYSTEM_INSTRUCTION = (
    "You are a task-scheduling assistant. Follow these rules every conversation:\n"
    "1. Call task.list_actions@v1 exactly once per thread before creating any task "
    "to discover available actions and their required parameters.\n"
    "2. When the user wants a task run now or does not mention timing, set "
    'schedule_type to "immediate".\n'
    '3. When no timezone is specified, default timezone to "UTC".\n'
    "4. Use the exact action name from the registry — never invent action names.\n"
    "5. On tool errors check error.code and error.field to self-correct before retrying."
)

_TASK_CREATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["echo"],
            "description": "Name of the registered action to execute.",
        },
        "action_params": {
            "type": "object",
            "description": "Action-specific parameters (see task.list_actions@v1).",
        },
        "schedule_type": {
            "type": "string",
            "enum": ["immediate"],
            "default": "immediate",
            "description": "When to run the task. Only 'immediate' is supported in v1.",
        },
        "idempotency_key": {
            "type": ["string", "null"],
            "default": None,
            "description": "Optional caller-supplied deduplication key.",
        },
        "timezone": {
            "type": "string",
            "default": "UTC",
            "description": "IANA timezone name; defaults to UTC.",
        },
    },
    "required": ["action", "action_params"],
    "additionalProperties": False,
}

_TASK_LIST_ACTIONS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}


def _build_action_list() -> list[dict[str, Any]]:
    return [
        {
            "name": handler.name,
            "description": handler.description,
            "timeout_seconds": handler.timeout_seconds,
            "params_schema": handler.params_model.model_json_schema(),
        }
        for handler in ACTION_REGISTRY.values()
    ]


def create_server(user_id: str) -> Server:
    """Return a fully configured, transport-agnostic MCP Server bound to *user_id*."""
    server = Server("task-scheduler", instructions=SYSTEM_INSTRUCTION)

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name="task.create@v1",
                description=(
                    "Create a new scheduled task. Returns {ok, data: {job_id, status}} "
                    "on success or {ok: false, error: {code, message, field, expected}} on failure."
                ),
                inputSchema=_TASK_CREATE_SCHEMA,
            ),
            types.Tool(
                name="task.status@v1",
                description=(
                    "Return the status of a single job. With include_runs=true, also "
                    "returns the most recent 10 execution runs. Returns NOT_FOUND for "
                    "unknown or cross-user job_id."
                ),
                inputSchema=_TASK_STATUS_SCHEMA,
            ),
            types.Tool(
                name="task.list_actions@v1",
                description=(
                    "List all registered actions with their descriptions, timeouts, "
                    "and parameter schemas. Call once per thread before task.create@v1."
                ),
                inputSchema=_TASK_LIST_ACTIONS_SCHEMA,
            ),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
        if name == "task.list_actions@v1":
            result = success({"actions": _build_action_list()})
        elif name == "task.create@v1":
            result = await _handle_task_create(arguments, user_id)
        elif name == "task.status@v1":
            result = await handle_task_status(arguments, user_id)
        else:
            result = error("INTERNAL", f"Unknown tool: {name}")
        return [types.TextContent(type="text", text=json.dumps(result))]

    return server


async def _handle_task_create(arguments: dict[str, Any], user_id: str) -> dict[str, Any]:
    action = arguments.get("action")
    action_params = arguments.get("action_params", {})
    schedule_type = arguments.get("schedule_type", "immediate")
    idempotency_key = arguments.get("idempotency_key")

    try:
        async with async_session_factory() as session:
            job = await create_job(
                session,
                user_id=user_id,
                action=action,
                action_params=action_params,
                schedule_type=schedule_type,
                idempotency_key=idempotency_key,
            )
        return success({"job_id": job.job_id, "status": "scheduled"})
    except Exception as exc:
        logger.exception("task.create@v1 failed for user %s", user_id)
        return map_domain_error(exc)
