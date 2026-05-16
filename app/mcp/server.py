"""Transport-agnostic MCP server: task.create.v1 + task.list_actions.v1 per ADR-006/014."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import mcp.types as types
from mcp import McpError
from mcp.server.lowlevel import Server
from mcp.types import INVALID_PARAMS, ErrorData
from pydantic import AnyUrl
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.actions.registry import ACTION_REGISTRY
from app.config.settings import settings
from app.db.engine import async_session_factory as default_session_factory
from app.domain.jobs import create_job
from app.mcp.envelope import error, success
from app.mcp.errors import map_domain_error
from app.mcp.handlers.cancel import TASK_CANCEL_SCHEMA, handle_task_cancel
from app.mcp.handlers.list import TASK_LIST_SCHEMA, handle_task_list
from app.mcp.handlers.status import TASK_STATUS_SCHEMA, handle_task_status
from app.mcp.prompts import daily_review as _daily_review
from app.mcp.prompts import setup_summary as _setup_summary
from app.mcp.resources.actions_resource import read_tasks_actions
from app.mcp.resources.job_resource import read_tasks_job
from app.mcp.resources.list_resource import read_tasks_list

logger = logging.getLogger(__name__)

_SYSTEM_INSTRUCTION_FILE = Path(__file__).parent / "system_instruction.md"
SYSTEM_INSTRUCTION: str = _SYSTEM_INSTRUCTION_FILE.read_text(encoding="utf-8").strip()

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
            "description": "Action-specific parameters (see task.list_actions.v1).",
        },
        "schedule_type": {
            "type": "string",
            "enum": ["immediate", "one-shot", "recurring"],
            "default": "immediate",
            "description": (
                "When to run the task. 'immediate' runs as soon as a worker is free; "
                "'one-shot' runs at the specified scheduled_at datetime; "
                "'recurring' runs on a cron schedule (requires cron_expr)."
            ),
        },
        "scheduled_at": {
            "type": ["string", "null"],
            "default": None,
            "description": (
                "ISO 8601 timezone-aware datetime for one-shot scheduling "
                "(e.g. '2026-05-14T09:00:00+00:00'). Required when schedule_type='one-shot'."
            ),
        },
        "cron_expr": {
            "type": ["string", "null"],
            "default": None,
            "description": (
                "5-field POSIX cron expression (minute hour dom month dow), "
                "e.g. '0 8 * * *' for daily at 8 AM. "
                "Also accepts @daily, @hourly, @weekly, @monthly, @yearly. "
                "Required when schedule_type='recurring'."
            ),
        },
        "idempotency_key": {
            "type": ["string", "null"],
            "default": None,
            "description": "Optional caller-supplied deduplication key.",
        },
        "timezone": {
            "type": ["string", "null"],
            "default": None,
            "description": (
                "IANA timezone key (e.g. 'Asia/Taipei', 'Europe/London', 'UTC'). "
                "Used to interpret cron schedules and naive scheduled_at datetimes. "
                "Falls back to the X-Timezone request header, then the MCP_USER_TZ "
                "environment variable, then UTC."
            ),
        },
        "trigger_on_job_id": {
            "type": ["integer", "null"],
            "default": None,
            "description": (
                "Job ID to chain on. When set, this job's first run starts in WAITING "
                "status and flips to PENDING (or CANCELLED) when the referenced job "
                "produces a terminal event matching trigger_on_status."
            ),
        },
        "trigger_on_status": {
            "type": ["string", "null"],
            "enum": ["SUCCEEDED", "FAILED", "ANY", None],
            "default": None,
            "description": (
                "Terminal status that unblocks this job. 'SUCCEEDED' (default), 'FAILED', "
                "or 'ANY' (matches every terminal event including CANCELLED). "
                "Only meaningful when trigger_on_job_id is set."
            ),
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


_RESOURCE_LIST = types.Resource(
    uri=types.AnyUrl("tasks://list"),
    name="Task List",
    description=(
        "Snapshot of the caller's most recent 20 jobs, newest-first. "
        "Snapshot taken at session start; call task.list.v1 for fresh data."
    ),
    mimeType="application/json",
)

_RESOURCE_ACTIONS = types.Resource(
    uri=types.AnyUrl("tasks://actions"),
    name="Action Registry",
    description=(
        "Available actions with their names, descriptions, timeouts, and parameter schemas."
    ),
    mimeType="application/json",
)

_RESOURCE_TEMPLATE_JOB = types.ResourceTemplate(
    uriTemplate="tasks://job/{job_id}",
    name="Job Detail",
    description="Full job definition plus the 5 most recent execution runs for a given job_id.",
    mimeType="application/json",
)


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


def create_server(
    user_id: str,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> Server:
    """Return a fully configured, transport-agnostic MCP Server bound to *user_id*.

    `session_factory` is injectable so tests can pass a per-test factory that
    avoids asyncpg cross-event-loop errors (each pytest-asyncio test gets its
    own loop). Defaults to the module-level factory at runtime.
    """
    factory = session_factory or default_session_factory
    server = Server("task-scheduler", instructions=SYSTEM_INSTRUCTION)

    @server.list_prompts()
    async def list_prompts() -> list[types.Prompt]:
        return [
            _daily_review.build_prompt(),
            _setup_summary.build_prompt(),
        ]

    @server.get_prompt()
    async def get_prompt(name: str, arguments: dict[str, str] | None) -> types.GetPromptResult:
        if name == _daily_review.NAME:
            return _daily_review.build_result()
        if name == _setup_summary.NAME:
            topic, schedule = _setup_summary.validate_args(arguments)
            return _setup_summary.build_result(topic, schedule)
        raise McpError(ErrorData(code=INVALID_PARAMS, message=f"Unknown prompt: {name}"))

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name="task.create.v1",
                description=(
                    "Create a new scheduled task. Returns {ok, data: {job_id, status}} "
                    "on success or {ok: false, error: {code, message, field, expected}} on failure."
                ),
                inputSchema=_TASK_CREATE_SCHEMA,
            ),
            types.Tool(
                name="task.status.v1",
                description=(
                    "Return the status of a single job. With include_runs=true, also "
                    "returns the most recent 10 execution runs. Returns NOT_FOUND for "
                    "unknown or cross-user job_id."
                ),
                inputSchema=TASK_STATUS_SCHEMA,
            ),
            types.Tool(
                name="task.cancel.v1",
                description=(
                    "Cancel a job. Pending, queued, and waiting runs are immediately "
                    "transitioned to CANCELLED. Runs that are currently in-flight (RUNNING) "
                    "are left to complete naturally — cancellation is best-effort for "
                    "currently-running executions. Re-cancelling an already-cancelled job "
                    "is idempotent and returns success. Returns INVALID_STATE if the job "
                    "already fully terminated (all runs SUCCEEDED or FAILED)."
                ),
                inputSchema=TASK_CANCEL_SCHEMA,
            ),
            types.Tool(
                name="task.list.v1",
                description=(
                    "List the caller's jobs newest-first. Supports status filter, "
                    "created_at range, and offset pagination (page + pageSize)."
                ),
                inputSchema=TASK_LIST_SCHEMA,
            ),
            types.Tool(
                name="task.list_actions.v1",
                description=(
                    "List all registered actions with their descriptions, timeouts, "
                    "and parameter schemas. Call once per thread before task.create.v1."
                ),
                inputSchema=_TASK_LIST_ACTIONS_SCHEMA,
            ),
        ]

    @server.list_resource_templates()
    async def list_resource_templates() -> list[types.ResourceTemplate]:
        return [_RESOURCE_TEMPLATE_JOB]

    @server.list_resources()
    async def list_resources() -> list[types.Resource]:
        return [_RESOURCE_LIST, _RESOURCE_ACTIONS]

    @server.read_resource()
    async def read_resource(uri: AnyUrl):
        if uri.scheme == "tasks":
            if uri.host == "list":
                return await read_tasks_list(user_id, session_factory=factory)
            if uri.host == "actions":
                return read_tasks_actions()
            if uri.host == "job":
                return await read_tasks_job(uri, user_id, session_factory=factory)
        raise McpError(ErrorData(code=INVALID_PARAMS, message=f"Unknown resource URI: {uri}"))

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
        if name == "task.list_actions.v1":
            result = success({"actions": _build_action_list()})
        elif name == "task.create.v1":
            result = await _handle_task_create(arguments, user_id, session_factory=factory)
        elif name == "task.status.v1":
            result = await handle_task_status(arguments, user_id, session_factory=factory)
        elif name == "task.cancel.v1":
            result = await handle_task_cancel(arguments, user_id, session_factory=factory)
        elif name == "task.list.v1":
            result = await handle_task_list(arguments, user_id, session_factory=factory)
        else:
            result = error("INTERNAL", f"Unknown tool: {name}")
        return [types.TextContent(type="text", text=json.dumps(result))]

    return server


async def _handle_task_create(
    arguments: dict[str, Any],
    user_id: str,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    tz_header: str | None = None,
) -> dict[str, Any]:
    action = arguments.get("action")
    action_params = arguments.get("action_params", {})
    schedule_type = arguments.get("schedule_type", "immediate")
    scheduled_at = arguments.get("scheduled_at")
    idempotency_key = arguments.get("idempotency_key")
    timezone = arguments.get("timezone") or None
    cron_expr = arguments.get("cron_expr") or None
    trigger_on_job_id_raw = arguments.get("trigger_on_job_id")
    trigger_on_status = arguments.get("trigger_on_status")

    trigger_on_job_id: int | None = None
    if trigger_on_job_id_raw is not None:
        try:
            trigger_on_job_id = int(trigger_on_job_id_raw)
        except (TypeError, ValueError):
            return error(
                "USER_INPUT",
                "trigger_on_job_id must be an integer",
                field="trigger_on_job_id",
                expected="integer",
            )

    try:
        async with session_factory() as session:
            job = await create_job(
                session,
                user_id=user_id,
                action=action,
                action_params=action_params,
                schedule_type=schedule_type,
                scheduled_at=scheduled_at,
                idempotency_key=idempotency_key,
                timezone=timezone,
                cron_expr=cron_expr,
                tz_header=tz_header,
                tz_env=settings.mcp_user_tz,
                trigger_on_job_id=trigger_on_job_id,
                trigger_on_status=trigger_on_status,
            )
        return success({"job_id": job.job_id, "status": "scheduled"})
    except Exception as exc:
        logger.exception("task.create.v1 failed for user %s", user_id)
        return map_domain_error(exc)
