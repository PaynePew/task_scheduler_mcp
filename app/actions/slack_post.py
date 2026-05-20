"""slack_post action handler — posts to a Slack incoming webhook.

Secrets convention (ADR-032): the webhook URL is read from ``SLACK_WEBHOOK_URL``
environment variable. It is NEVER stored in ``action_params``. Callers may pass
``${SLACK_WEBHOOK_URL}`` as a template reference in other string params; the
secrets resolver expands it before network I/O.

Chain-fed mode (ADR-033): set ``from_run_id`` to consume a prior handler's
``JobRun.result`` as the message body. The upstream payload is dispatched via
``UpstreamPayload`` variants — ok-path renders content, error-path renders a ⚠
alert using the chosen template.

Templates:
    raw            — passes message text unchanged (default)
    digest_v1      — formats structured upstream dict as a bulleted digest
    interview_brief — formats upstream dict as key/value brief sections

Error classification:
    429            → retryable (Slack rate limit)
    401/403/404/410 → not retryable (auth / channel error → DLQ)
    5xx            → retryable (Slack server error)
    timeout / network → retryable

Manual smoke test::

    SLACK_WEBHOOK_URL=<your-webhook-url> uv run python -c "
    import asyncio
    from app.actions.slack_post import SlackPostHandler, SlackPostParams
    p = SlackPostParams(channel='#general', message='Hello from slack_post smoke test')
    r = asyncio.run(SlackPostHandler().execute(run=None, params=p))
    print(r)"
"""

from __future__ import annotations

import os
from collections.abc import Callable
from enum import StrEnum
from typing import Any, ClassVar

import httpx
from pydantic import BaseModel

from app.actions.base import ActionResult, CredentialMode
from app.chain.upstream_reader import (
    InvalidJson,
    NoResult,
    Ok,
    UpstreamError,
    read_upstream,
)
from app.secrets.resolver import SecretResolutionError, build_effective_whitelist, resolve

# 429: rate-limited → retry.  401/403/404/410: permanent auth/channel error → DLQ.
# 5xx: Slack-side error → retry. DLQ statuses fall through to retryable=False
# since they are not in this set; no explicit DLQ set is needed.
_RETRYABLE_STATUSES: frozenset[int] = frozenset([429, *range(500, 600)])


class SlackTemplate(StrEnum):
    raw = "raw"
    digest_v1 = "digest_v1"
    interview_brief = "interview_brief"


class SlackPostParams(BaseModel):
    channel: str
    message: str | None = None
    from_run_id: int | None = None
    template: SlackTemplate | None = None


# ---------------------------------------------------------------------------
# Template formatters
# ---------------------------------------------------------------------------


def _format_raw(data: Any, *, is_error: bool = False) -> str:
    if is_error:
        return f"⚠ Upstream error: {data}"
    return str(data)


def _format_digest_v1(data: Any, *, is_error: bool = False) -> str:
    if is_error:
        return f"⚠ *Digest unavailable* — upstream error: {data}"
    if not isinstance(data, dict):
        return f"*Daily Digest*\n{data}"
    lines = ["*Daily Digest*"]
    for key, value in data.items():
        lines.append(f"• *{key}*: {value}")
    return "\n".join(lines)


def _format_interview_brief(data: Any, *, is_error: bool = False) -> str:
    if is_error:
        return f"⚠ *Interview brief unavailable* — upstream error: {data}"
    if not isinstance(data, dict):
        return f"*Interview Brief*\n{data}"
    lines = ["*Interview Brief*"]
    for key, value in data.items():
        lines.append(f"*{key}*\n{value}")
    return "\n".join(lines)


_TEMPLATE_FORMATTERS: dict[SlackTemplate, Callable[..., str]] = {
    SlackTemplate.raw: _format_raw,
    SlackTemplate.digest_v1: _format_digest_v1,
    SlackTemplate.interview_brief: _format_interview_brief,
}


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class SlackPostHandler:
    """Posts a message to a Slack channel via incoming webhook.

    Pass *session_factory* to override the default DB session factory — useful
    in tests that need to inject a pre-seeded session without touching the real
    engine pool.
    """

    name: ClassVar[str] = "slack_post"
    description: ClassVar[str] = (
        "Posts a message to a Slack channel via incoming webhook. "
        "Set SLACK_WEBHOOK_URL in the server environment. "
        "Use from_run_id to chain from a prior handler's output. "
        "Templates: raw (default), digest_v1, interview_brief."
    )
    params_model: ClassVar[type[BaseModel]] = SlackPostParams
    timeout_seconds: ClassVar[int] = 30
    requires_operator: ClassVar[bool] = False
    credential_mode: ClassVar[CredentialMode] = CredentialMode.operator_env

    def __init__(self, session_factory: Any = None) -> None:
        self._session_factory = session_factory

    def _get_session_factory(self) -> Any:
        if self._session_factory is not None:
            return self._session_factory
        from app.db.engine import async_session_factory

        return async_session_factory

    async def execute(self, run: Any, params: SlackPostParams) -> ActionResult:
        # Resolve any ${VAR} references (secrets convention, ADR-032).
        whitelist = build_effective_whitelist()
        env = dict(os.environ)
        try:
            resolved_channel = resolve(params.channel, env, whitelist)
            resolved_message = (
                resolve(params.message, env, whitelist) if params.message is not None else None
            )
        except SecretResolutionError as exc:
            return ActionResult(ok=False, result=None, error=str(exc), retryable=False)

        # Webhook URL comes from env — never from action_params.
        webhook_url = os.environ.get("SLACK_WEBHOOK_URL")
        if not webhook_url:
            return ActionResult(
                ok=False,
                result=None,
                error="SLACK_WEBHOOK_URL environment variable is not set",
                retryable=False,
            )

        message_text = await self._build_message(
            params=params,
            resolved_message=resolved_message,
        )
        if isinstance(message_text, ActionResult):
            return message_text

        payload = {"text": message_text, "channel": resolved_channel}

        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(webhook_url, json=payload)
        except httpx.TimeoutException as exc:
            return ActionResult(ok=False, result=None, error=f"Timeout: {exc}", retryable=True)
        except httpx.RequestError as exc:
            return ActionResult(ok=False, result=None, error=str(exc), retryable=True)

        return self._classify_response(response)

    async def _build_message(
        self,
        params: SlackPostParams,
        resolved_message: str | None,
    ) -> str | ActionResult:
        template = params.template or SlackTemplate.raw
        formatter = _TEMPLATE_FORMATTERS[template]

        if params.from_run_id is not None:
            return await self._message_from_upstream(params.from_run_id, formatter)

        if resolved_message is not None:
            return resolved_message

        return ActionResult(
            ok=False,
            result=None,
            error="Either message or from_run_id must be provided",
            retryable=False,
        )

    async def _message_from_upstream(
        self, from_run_id: int, formatter: Callable[..., str]
    ) -> str | ActionResult:
        factory = self._get_session_factory()
        async with factory() as session:
            async with session.begin():
                upstream = await read_upstream(run_id=from_run_id, session=session)

        if isinstance(upstream, Ok):
            return formatter(upstream.data, is_error=False)
        if isinstance(upstream, UpstreamError):
            return formatter(upstream.error_msg, is_error=True)
        if isinstance(upstream, NoResult):
            return formatter("(no result)", is_error=True)
        if isinstance(upstream, InvalidJson):
            return formatter(f"(invalid JSON: {upstream.raw[:100]})", is_error=True)
        return ActionResult(
            ok=False, result=None, error="unknown upstream payload", retryable=False
        )

    @staticmethod
    def _classify_response(response: httpx.Response) -> ActionResult:
        status = response.status_code
        body = response.text[:200]

        if response.is_success:
            return ActionResult(
                ok=True,
                result={"status_code": status, "body": body},
                error=None,
                retryable=False,
            )

        retryable = status in _RETRYABLE_STATUSES
        return ActionResult(
            ok=False,
            result=None,
            error=f"Slack webhook returned HTTP {status}: {body}",
            retryable=retryable,
        )
