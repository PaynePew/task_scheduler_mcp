"""slack_post action handler — posts to a Slack channel via the Web API.

Credential resolution (ADR-050, ADR-051):
  credential_mode = oauth_connection — resolves the Slack bot token from the
  connection store keyed by (job.user_id, "slack").  The session_factory and
  kms_envelope are injected at construction time; defaults to module-level singletons.

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
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.actions.base import ActionResult, CredentialMode
from app.chain.upstream_reader import (
    InvalidJson,
    NoResult,
    Ok,
    UpstreamError,
    read_upstream,
)
from app.connections.store import ConnectionMiss, ConnectionStore
from app.crypto.envelope_factory import kms_envelope_from_settings as _make_default_kms_envelope
from app.crypto.kms_envelope import KmsEnvelope

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

    Resolves the bot token from the connection store keyed by (run.user_id, "slack").
    Pass *session_factory* and *kms_envelope* to override module-level defaults —
    useful in tests that need to inject pre-seeded sessions without touching the
    real engine pool or real AWS KMS.
    """

    name: ClassVar[str] = "slack_post"
    description: ClassVar[str] = (
        "Posts a message to a Slack channel using your connected Slack workspace. "
        "Connect your Slack account at /connections. "
        "Use from_run_id to chain from a prior handler's output. "
        "Templates: raw (default), digest_v1, interview_brief."
    )
    params_model: ClassVar[type[BaseModel]] = SlackPostParams
    timeout_seconds: ClassVar[int] = 30
    requires_operator: ClassVar[bool] = False
    credential_mode: ClassVar[CredentialMode] = CredentialMode.oauth_connection
    required_provider: ClassVar[str] = "slack"

    def __init__(
        self,
        session_factory: async_sessionmaker | None = None,
        kms_envelope: KmsEnvelope | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._kms_envelope = kms_envelope

    def _get_session_factory(self) -> async_sessionmaker:
        if self._session_factory is not None:
            return self._session_factory
        from app.db.engine import async_session_factory  # noqa: PLC0415

        return async_session_factory

    def _get_kms_envelope(self) -> KmsEnvelope | None:
        if self._kms_envelope is not None:
            return self._kms_envelope
        return _make_default_kms_envelope()

    async def execute(self, run: Any, params: SlackPostParams) -> ActionResult:
        user_id: str = run.user_id

        # Look up the Slack token from the connection store.
        envelope = self._get_kms_envelope()
        if envelope is None:
            return ActionResult(
                ok=False,
                result=None,
                error=(
                    "slack_post: KMS not configured — cannot decrypt Slack connection. "
                    "Set KMS_KEY_ID in environment."
                ),
                retryable=False,
            )

        factory = self._get_session_factory()
        try:
            async with factory() as session:
                store = ConnectionStore(session, envelope)
                token = await store.get_fresh_token(user_id, "slack")
        except ConnectionMiss:
            from app.config.settings import settings  # noqa: PLC0415

            connect_url = f"{settings.connections_base_url}/connections"
            return ActionResult(
                ok=False,
                result=None,
                error=(
                    f"No Slack connection for this user. "
                    f"Connect your Slack workspace at {connect_url}"
                ),
                retryable=False,
            )

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
