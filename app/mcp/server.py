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

from app.actions.base import ActionHandler, CredentialMode
from app.actions.registry import ACTION_REGISTRY
from app.config.settings import settings
from app.connections.store import ConnectionMiss, ConnectionStore
from app.crypto.envelope_factory import kms_envelope_from_settings as _make_server_kms_envelope
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
from app.mcp.resources.recent_results import read_tasks_recent_results
from app.overload.health import get_queue_depth
from app.quotas.containment import Allow as ContainmentAllow
from app.quotas.containment import ContainmentLimits, check_containment
from app.ratelimit.checker import Allow, RateLimits, check_rate_limit
from app.secrets.literal_detection import detect_literal_secret

logger = logging.getLogger(__name__)

_SYSTEM_INSTRUCTION_FILE = Path(__file__).parent / "system_instruction.md"


_ACTIONS_BLOCK_PLACEHOLDER = "{ACTIONS_BLOCK}"


def build_system_instruction(registry: dict[str, ActionHandler], template: str) -> str:
    """Compose the MCP `instructions` string by injecting an action listing into the template.

    The template must contain the placeholder ``{ACTIONS_BLOCK}``; absence raises
    ``ValueError`` so a typo in the markdown surfaces at import time, not as a
    silently-empty instructions string. Each registered action contributes one
    bullet line; the listing is sorted by action name for deterministic output
    (so the same registry always produces the same string).

    See ADR-061. Derived rather than hand-maintained — adding a handler to
    ``ACTION_REGISTRY`` automatically surfaces it here without a separate edit.
    """

    if _ACTIONS_BLOCK_PLACEHOLDER not in template:
        raise ValueError(
            f"system instruction template missing {_ACTIONS_BLOCK_PLACEHOLDER} placeholder"
        )
    lines: list[str] = []
    for handler in sorted(registry.values(), key=lambda h: h.name):
        # Operator-only actions (ADR-051) are deliberately omitted from the
        # cold-start instructions so they are not advertised to delegated users
        # (ADR-066). They remain fully functional and discoverable via
        # task.list_actions.v1. getattr keeps test stub handlers (no
        # requires_operator attr) working — they default to public.
        if getattr(handler, "requires_operator", False):
            continue
        suffix = f" (needs {handler.required_provider} OAuth)" if handler.required_provider else ""
        lines.append(f"- {handler.name}{suffix} -- {handler.summary_line}")
    return template.replace(_ACTIONS_BLOCK_PLACEHOLDER, "\n".join(lines))


SYSTEM_INSTRUCTION: str = build_system_instruction(
    ACTION_REGISTRY,
    _SYSTEM_INSTRUCTION_FILE.read_text(encoding="utf-8").strip(),
)


async def _load_user_connected_providers(
    user_id: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> set[str]:
    """Return the set of provider names that *user_id* has a connection for.

    Returns an empty set if KMS is not configured (dev/CI mode) or on any error.
    This is a single DB query regardless of how many OAuth actions are in the registry.
    """
    envelope = _make_server_kms_envelope()
    if envelope is None:
        return set()
    try:
        async with session_factory() as session:
            store = ConnectionStore(session, envelope)
            infos = await store.list(user_id)
        # Presence-based: a stored connection counts as connected even if its
        # access token has expired — expiry is recovered via transparent refresh
        # at execute time (ADR-050/061). The prior `expires_at > now` filter gave
        # a false `not_connected` for Google (~1h access-token TTL), contradicting
        # both the real send path and the presence-based /connections web page.
        return {info.provider for info in infos}
    except Exception:
        logger.exception("failed to load connected providers for user %s", user_id)
        return set()


async def _check_oauth_connection(
    action: str | None,
    user_id: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> dict[str, Any] | None:
    """Return an error envelope if the action requires an OAuth connection the user lacks.

    Returns None if the action does not require a connection, or the user has one,
    or KMS is not configured (skip the check in dev/test environments).
    """
    if action is None:
        return None
    handler = ACTION_REGISTRY.get(action)
    if handler is None or handler.credential_mode != CredentialMode.oauth_connection:
        return None
    provider = getattr(handler, "required_provider", None)
    if not provider:
        return None

    envelope = _make_server_kms_envelope()
    if envelope is None:
        # KMS not configured — skip the check (dev / CI mode).
        return None

    try:
        async with session_factory() as session:
            store = ConnectionStore(session, envelope)
            await store.get_fresh_token(user_id, provider)
    except ConnectionMiss:
        connect_url = f"{settings.connections_base_url}/connections"
        return error(
            "MISSING_CONNECTION",
            f"This action requires a {provider} connection that you haven't set up yet. "
            f"Connect your {provider.capitalize()} account at {connect_url}",
            connect_url=connect_url,
        )
    except Exception:
        # Any other error (KMS unreachable, DB error) — don't block job creation.
        logger.exception("oauth connection pre-flight failed for user %s", user_id)
        return None

    return None


_TASK_CREATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": sorted(ACTION_REGISTRY.keys()),
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
                "Job ID to chain on. When set, this job has no run until the "
                "referenced job produces a terminal event matching trigger_on_status; "
                "that event creates this job's first run (continuation)."
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

_RESOURCE_RECENT_RESULTS = types.Resource(
    uri=types.AnyUrl("tasks://recent-results"),
    name="Recent Results",
    description=(
        "Last 24h of completed/failed/cancelled runs for the calling user. "
        "Capped at 50 rows newest-first. Use this on connect to brief the user "
        "on overnight activity (ADR-037)."
    ),
    mimeType="application/json",
)


def _build_action_list(
    connected_providers: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Build the action list payload, enriched with auth metadata (Layer 2, ADR-061).

    *connected_providers* is the set of provider names the calling user already
    has a valid connection for. Pass ``None`` (or an empty set) when connection
    status is unavailable — OAuth actions will show ``"not_connected"``.
    """
    if connected_providers is None:
        connected_providers = set()
    connect_url_base = f"{settings.connections_base_url}/connections"
    actions = []
    for handler in ACTION_REGISTRY.values():
        auth_required = handler.credential_mode == CredentialMode.oauth_connection
        if not auth_required:
            auth_status = "n/a"
            connect_url = None
        elif handler.required_provider and handler.required_provider in connected_providers:
            auth_status = "connected"
            connect_url = connect_url_base
        else:
            auth_status = "not_connected"
            connect_url = connect_url_base
        actions.append(
            {
                "name": handler.name,
                "description": handler.description,
                "timeout_seconds": handler.timeout_seconds,
                "params_schema": handler.params_model.model_json_schema(),
                "auth_required": auth_required,
                "auth_status": auth_status,
                "connect_url": connect_url,
            }
        )
    return actions


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
    # Routing identifier (ADR-066): leads with the distinctive token "owl" so the
    # LLM does not confuse this with a built-in/first-party scheduler. Display name
    # stays "Owl Task Scheduler MCP"; infra identity stays task_scheduler_mcp.
    server = Server("owl-scheduler", instructions=SYSTEM_INSTRUCTION)

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
                    "Create a task that runs a registered action (see task.list_actions.v1) "
                    "immediately, once at a future time, or on a recurring cron schedule; "
                    "supports chaining and cancellation. Returns {ok, data: {job_id, status}} "
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
                    "and parameter schemas. Call once per thread before task.create.v1. "
                    "Each action also carries `auth_required`, `auth_status`, and "
                    "`connect_url` so you can tell the user when to visit /connections "
                    "before calling task.create.v1."
                ),
                inputSchema=_TASK_LIST_ACTIONS_SCHEMA,
            ),
        ]

    @server.list_resource_templates()
    async def list_resource_templates() -> list[types.ResourceTemplate]:
        return [_RESOURCE_TEMPLATE_JOB]

    @server.list_resources()
    async def list_resources() -> list[types.Resource]:
        return [_RESOURCE_LIST, _RESOURCE_ACTIONS, _RESOURCE_RECENT_RESULTS]

    @server.read_resource()
    async def read_resource(uri: AnyUrl):
        if uri.scheme == "tasks":
            if uri.host == "list":
                return await read_tasks_list(user_id, session_factory=factory)
            if uri.host == "actions":
                return read_tasks_actions()
            if uri.host == "job":
                return await read_tasks_job(uri, user_id, session_factory=factory)
            if uri.host == "recent-results":
                return await read_tasks_recent_results(user_id, session_factory=factory)
        raise McpError(ErrorData(code=INVALID_PARAMS, message=f"Unknown resource URI: {uri}"))

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
        if name == "task.list_actions.v1":
            connected = await _load_user_connected_providers(user_id, factory)
            result = success({"actions": _build_action_list(connected)})
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
    _queue_depth: int | None = None,
) -> dict[str, Any]:
    action = arguments.get("action")
    action_params = arguments.get("action_params", {})

    matched_prefix = detect_literal_secret(action_params)
    if matched_prefix is not None:
        return error(
            "USER_INPUT",
            (
                f"action_params appears to contain a literal credential"
                f" (matched prefix: {matched_prefix!r})."
                " Use ${VAR_NAME} form instead (e.g. ${ANTHROPIC_API_KEY})"
                " so the value is read from the server environment at execution time."
            ),
            field="action_params",
            expected="${VAR_NAME} reference instead of a literal credential",
        )

    # ------------------------------------------------------------------
    # SQS backpressure: if the queue is deep, slow down task creation
    # to avoid overwhelming the worker pool (ADR-057).
    # _queue_depth is injectable for unit-test determinism.
    # ------------------------------------------------------------------
    backpressure_threshold = settings.overload_backpressure_queue_depth
    depth = _queue_depth if _queue_depth is not None else get_queue_depth(settings.queue_url)
    if depth >= backpressure_threshold:
        retry_after = settings.overload_retry_after_seconds
        return error(
            "INVALID_STATE",
            f"Queue depth ({depth}) exceeds threshold ({backpressure_threshold}). "
            f"The system is busy; please retry after {retry_after} seconds.",
            field="queue_depth",
            expected={"retry_after_seconds": retry_after},
        )

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
        is_operator = bool(settings.operator_user_id) and user_id == settings.operator_user_id

        if not is_operator:
            limits = RateLimits(
                daily=settings.rate_limit_daily,
                burst=settings.rate_limit_burst_per_minute,
            )
            async with session_factory() as rl_session:
                decision = await check_rate_limit(user_id, rl_session, limits)
            if not isinstance(decision, Allow):
                return error(
                    "USER_INPUT",
                    f"Rate limit exceeded: {decision.reason}",
                    field="user_id",
                    expected={"retry_after_seconds": decision.retry_after_seconds},
                )

            containment_limits = ContainmentLimits(
                active_recurring_per_user=settings.quota_active_recurring_per_user,
                active_total_per_user=settings.quota_active_total_per_user,
                global_active_recurring=settings.quota_global_active_recurring,
            )
            async with session_factory() as ct_session:
                ct_decision = await check_containment(user_id, ct_session, containment_limits)
            if not isinstance(ct_decision, ContainmentAllow):
                return error(
                    "USER_INPUT",
                    f"Quota exceeded: {ct_decision.reason}",
                    field="user_id",
                    expected={"retry_after_seconds": ct_decision.retry_after_seconds},
                )

        # ------------------------------------------------------------------
        # OAuth connection pre-flight (ADR-058): if the action requires an
        # oauth_connection and the user hasn't set one up, surface connect_url
        # so the LLM/client can hand the user a direct link to /connections.
        # Only checked when KMS is configured; skipped in dev/test mode.
        # ------------------------------------------------------------------
        if not is_operator:
            conn_check = await _check_oauth_connection(action, user_id, session_factory)
            if conn_check is not None:
                return conn_check

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
                operator_user_id=settings.operator_user_id,
            )
        return success({"job_id": job.job_id, "status": "scheduled"})
    except Exception as exc:
        logger.exception("task.create.v1 failed for user %s", user_id)
        return map_domain_error(exc)
