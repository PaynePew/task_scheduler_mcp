"""slack_post action handler — posts to a Slack channel via the Web API.

Credential resolution (ADR-050, ADR-051):
  credential_mode = oauth_connection — resolves the Slack bot token via
  the get_token facade keyed by (job.user_id, "slack").

If the user has no Slack connection:
  - execute() returns ok=False, retryable=False with a descriptive error + connect_url.
  - task.create pre-flight also checks and returns an error with connect_url
    (handled in app.mcp.server, not here).

Chain-fed mode (ADR-033): set ``from_run_id`` to consume a prior handler's
``JobRun.result`` as the message body. The upstream payload is dispatched via
``UpstreamPayload`` variants — ok-path renders content, error-path renders a ⚠
alert using the chosen template.

Templates:
    raw            — passes message text unchanged (default)
    digest_v1      — formats structured upstream dict as a bulleted digest
    interview_brief — formats upstream dict as key/value brief sections

Error classification:
    HTTP 429             → retryable (rate limit)
    HTTP 5xx             → retryable (Slack server error)
    ok=false error=ratelimited → retryable
    ok=false (other)     → not retryable (auth / channel / message error)
    timeout / network    → retryable
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from typing import Any, ClassVar

import httpx
from pydantic import BaseModel

from app.actions._oauth import check_oauth_for_execute, missing_connection_result
from app.actions.base import ActionResult, CredentialMode
from app.chain.upstream_reader import resolve_for_display
from app.connections.store import ConnectionMiss, get_token

_SLACK_API_BASE = "https://slack.com/api"

_NON_RETRYABLE_SLACK_ERRORS: frozenset[str] = frozenset(
    [
        "invalid_auth",
        "token_revoked",
        "not_authed",
        "account_inactive",
        "channel_not_found",
        "not_in_channel",
        "is_archived",
        "message_too_long",
        "no_text",
        "invalid_blocks",
    ]
)


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
    """Posts a message to a Slack channel via the Slack Web API.

    Resolves the bot token via get_token(user_id, "slack") from the connection
    store. Patch app.actions.slack_post.get_token in tests.
    """

    name: ClassVar[str] = "slack_post"
    description: ClassVar[str] = (
        "Posts a message to a Slack channel using your connected Slack workspace. "
        "Connect your Slack account at /connections. "
        "Use from_run_id to chain from a prior handler's output. "
        "Templates: raw (default), digest_v1, interview_brief."
    )
    summary_line: ClassVar[str] = (
        "Posts a message to a Slack channel using the user's connected workspace."
    )
    params_model: ClassVar[type[BaseModel]] = SlackPostParams
    timeout_seconds: ClassVar[int] = 30
    requires_operator: ClassVar[bool] = False
    credential_mode: ClassVar[CredentialMode] = CredentialMode.oauth_connection
    required_provider: ClassVar[str | None] = "slack"

    async def execute(self, run: Any, params: SlackPostParams) -> ActionResult:
        # Check connection validity (expiry + missing) before attempting the action.
        oauth_err = await check_oauth_for_execute(run.user_id, "slack")
        if oauth_err is not None:
            return oauth_err

        try:
            token = await get_token(run.user_id, "slack")
        except ConnectionMiss:
            return missing_connection_result("slack")

        message_text = await self._build_message(params=params)
        if isinstance(message_text, ActionResult):
            return message_text

        payload = {"channel": params.channel, "text": message_text}

        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(
                    f"{_SLACK_API_BASE}/chat.postMessage",
                    headers={"Authorization": f"Bearer {token}"},
                    json=payload,
                )
        except httpx.TimeoutException as exc:
            return ActionResult(ok=False, result=None, error=f"Timeout: {exc}", retryable=True)
        except httpx.RequestError as exc:
            return ActionResult(ok=False, result=None, error=str(exc), retryable=True)

        return self._classify_response(response)

    async def _build_message(self, params: SlackPostParams) -> str | ActionResult:
        template = params.template or SlackTemplate.raw
        formatter = _TEMPLATE_FORMATTERS[template]

        if params.from_run_id is not None:
            return await self._message_from_upstream(params.from_run_id, formatter)

        if params.message is not None:
            return params.message

        return ActionResult(
            ok=False,
            result=None,
            error="Either message or from_run_id must be provided",
            retryable=False,
        )

    async def _message_from_upstream(self, from_run_id: int, formatter: Callable[..., str]) -> str:
        from app.db.engine import async_session_factory  # noqa: PLC0415

        async with async_session_factory() as session:
            async with session.begin():
                return await resolve_for_display(from_run_id, session, formatter=formatter)

    @staticmethod
    def _classify_response(response: httpx.Response) -> ActionResult:
        # HTTP-level rate limit / server error
        if response.status_code == 429:
            return ActionResult(
                ok=False,
                result=None,
                error="Slack rate-limited (HTTP 429)",
                retryable=True,
            )
        if response.status_code >= 500:
            return ActionResult(
                ok=False,
                result=None,
                error=f"Slack server error (HTTP {response.status_code})",
                retryable=True,
            )
        if not response.is_success:
            return ActionResult(
                ok=False,
                result=None,
                error=f"Slack API HTTP {response.status_code}",
                retryable=False,
            )

        # Slack Web API returns HTTP 200 with ok: bool in JSON
        try:
            body = response.json()
        except ValueError:
            # httpx raises json.JSONDecodeError (a ValueError subclass) for
            # non-JSON bodies. Treat as a permanent failure — the response
            # bytes are not Slack's API contract.
            return ActionResult(
                ok=False,
                result=None,
                error="Slack API returned non-JSON response",
                retryable=False,
            )

        if body.get("ok"):
            return ActionResult(
                ok=True,
                result={"channel": body.get("channel"), "ts": body.get("ts")},
                error=None,
                retryable=False,
            )

        error_code = body.get("error", "unknown")
        # "ratelimited" is the only known retryable error code; any other unknown
        # error_code also falls through to retryable so transient Slack issues
        # we haven't catalogued get another attempt.
        retryable = error_code not in _NON_RETRYABLE_SLACK_ERRORS
        return ActionResult(
            ok=False,
            result=None,
            error=f"Slack API error: {error_code}",
            retryable=retryable,
        )
