"""email_send action handler — sends email via the caller's connected Gmail.

Credential model (ADR-050, amended): email_send is OAuth/Gmail-only. It sends
via the Gmail API using the caller's Google OAuth token, fetched with
get_token(user_id, "google") (provider="google"). A user with no Google
connection gets MISSING_CONNECTION (connect at /connections); there is no SMTP
fallback.

Chain-fed mode (ADR-033): set from_run_id to consume a prior handler's
JobRun.result as the email body. Supports templates (raw, digest_v1).

Gmail API error classification:
    401 Unauthorized                → retryable=False (reconnect at /connections)
    403 Forbidden                   → retryable=False (insufficient scope)
    429 Rate limited                → retryable=True
    5xx Server Error                → retryable=True
    network / timeout               → retryable=True
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from enum import StrEnum
from typing import Any, ClassVar

import httpx
from pydantic import BaseModel, EmailStr

from app.actions._oauth import check_oauth_for_execute, missing_connection_result
from app.actions.base import ActionResult, CredentialMode
from app.actions.send_dedup import (
    DedupStore,
    PostgresDedupStore,
    SendDecision,
    derive_idempotency_key,
)
from app.chain.upstream_reader import resolve_for_display
from app.config.settings import settings
from app.connections.store import ConnectionMiss, get_token

logger = logging.getLogger(__name__)

_GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"


class EmailTemplate(StrEnum):
    raw = "raw"
    digest_v1 = "digest_v1"


class EmailSendParams(BaseModel):
    to: list[EmailStr]
    subject: str
    body: str | None = None
    from_run_id: int | None = None
    template: EmailTemplate | None = None


# ---------------------------------------------------------------------------
# Template formatters
# ---------------------------------------------------------------------------


def _format_raw(data: Any, *, is_error: bool = False) -> str:
    if is_error:
        return f"Upstream error: {data}"
    return str(data)


def _format_digest_v1(data: Any, *, is_error: bool = False) -> str:
    if is_error:
        return f"Upstream error: {data}"
    if not isinstance(data, dict):
        return f"Daily Digest\n\n{data}"
    lines = ["Daily Digest", ""]
    for key, value in data.items():
        lines.append(f"- {key}: {value}")
    return "\n".join(lines)


_TEMPLATE_FORMATTERS: dict[EmailTemplate, Callable[..., str]] = {
    EmailTemplate.raw: _format_raw,
    EmailTemplate.digest_v1: _format_digest_v1,
}


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class EmailSendHandler:
    """Sends email via the Gmail API using the caller's Google OAuth connection.

    The user must have a Google connection (provider="google") stored in the
    connection store (connect at /connections); otherwise the send returns
    MISSING_CONNECTION. There is no SMTP fallback (ADR-050, amended).

    email_send is non-idempotent (idempotent=False), so it is made
    effectively-once (ADR-070): a run-derived idempotency key + a write-ahead
    intent record (``dedup_store``) mean a redelivered or reconciler-retried
    send is a no-op at our boundary instead of a duplicate. Inject an in-memory
    ``dedup_store`` in unit tests; production uses the Postgres-backed default.

    Patch app.actions.email_send.get_token in tests to override OAuth lookup.
    """

    def __init__(self, dedup_store: DedupStore | None = None) -> None:
        self._dedup = dedup_store or PostgresDedupStore()

    name: ClassVar[str] = "email_send"
    description: ClassVar[str] = (
        "Sends a transactional email via Gmail using your connected Google "
        "account (connect at /connections). "
        "Use from_run_id to chain from a prior handler's output. "
        "Templates: raw (default), digest_v1."
    )
    summary_line: ClassVar[str] = (
        "Sends an email via the user's connected Gmail; supports digest_v1 chaining."
    )
    params_model: ClassVar[type[BaseModel]] = EmailSendParams
    timeout_seconds: ClassVar[int] = 30
    requires_operator: ClassVar[bool] = False
    credential_mode: ClassVar[CredentialMode] = CredentialMode.oauth_connection
    required_provider: ClassVar[str | None] = "google"
    idempotent: ClassVar[bool] = False  # external side effect (sends real email)

    async def execute(self, run: Any, params: EmailSendParams) -> ActionResult:
        # email_send is a PUBLIC action (requires_operator=False): it must NOT do
        # ${VAR} operator-secret substitution (ADR-050 dual-credential model, ADR-052
        # §5). Public users send via their own OAuth/Gmail; subject/body are literal.
        # A caller who types "${GITHUB_TOKEN}" gets that literal text, not the secret.
        #
        # Build email body (direct or chain-fed). Chain reads are scoped to the
        # caller's own runs (run.user_id) to block cross-tenant from_run_id reads.
        body_text = await self._build_body(params, params.body, user_id=run.user_id)
        if isinstance(body_text, ActionResult):
            return body_text

        # Send via the caller's connected Gmail. Returns MISSING_CONNECTION when
        # the user has no Google connection — there is no SMTP fallback.
        return await self._send_via_gmail(run, params, params.subject, body_text)

    async def _send_via_gmail(
        self,
        run: Any,
        params: EmailSendParams,
        subject: str,
        body_text: str,
    ) -> ActionResult:
        user_id = run.user_id
        if not user_id:
            return ActionResult(
                ok=False,
                result=None,
                error="email_send: no user_id on run — cannot resolve Google connection",
                retryable=False,
            )

        # Check connection validity (expiry + missing) before attempting the action.
        refresher = _make_google_refresher()
        oauth_err = await check_oauth_for_execute(user_id, "google", refresher=refresher)
        if oauth_err is not None:
            return oauth_err

        try:
            token = await get_token(user_id, "google", refresher=refresher)
        except ConnectionMiss:
            return missing_connection_result("google")

        # Build RFC 2822 message and encode as base64url for the Gmail API.
        # Defense-in-depth against header injection: strip CR/LF from the subject
        # so a newline can never inject a second header (e.g. "\r\nBcc: ..."). The
        # stdlib EmailMessage policy already rejects CR/LF in headers with a
        # ValueError; stripping first turns that into a clean send. (To is
        # EmailStr-validated; body via set_content is a body, not a header.)
        safe_subject = subject.replace("\r", " ").replace("\n", " ")
        msg = EmailMessage()
        msg["To"] = ", ".join(str(addr) for addr in params.to)
        msg["Subject"] = safe_subject
        msg.set_content(body_text)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()

        # Effectively-once gate (ADR-070): write-ahead the intent immediately
        # before the provider call. A key already marked 'sent' short-circuits to
        # a dedup no-op — the redelivered/retried run does not send a second copy.
        key = derive_idempotency_key(self.name, run.run_id)
        gate = await self._dedup.begin(key, run.run_id)
        if gate.decision is SendDecision.skip:
            logger.info(
                "email_send: dedup hit for run_id=%s (already sent); skipping Gmail send",
                run.run_id,
            )
            return self._ok_result(params, subject, gate.provider_message_id, deduped=True)
        if gate.decision is SendDecision.resend:
            logger.warning(
                "email_send: re-sending run_id=%s after an unconfirmed prior attempt"
                " (at-least-once bias; rare duplicate possible)",
                run.run_id,
            )

        try:
            async with httpx.AsyncClient(timeout=float(self.timeout_seconds)) as client:
                resp = await client.post(
                    _GMAIL_SEND_URL,
                    headers={"Authorization": f"Bearer {token}"},
                    json={"raw": raw},
                )
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            return ActionResult(ok=False, result=None, error=str(exc), retryable=True)

        if resp.status_code == 401:
            return ActionResult(
                ok=False,
                result=None,
                error=(
                    "Gmail API 401 Unauthorized — the stored Google connection is invalid "
                    "or revoked; reconnect at /connections"
                ),
                retryable=False,
            )
        if resp.status_code == 403:
            return ActionResult(
                ok=False,
                result=None,
                error="Gmail API 403 Forbidden — insufficient scope or quota exceeded",
                retryable=False,
            )
        if resp.status_code == 429:
            return ActionResult(
                ok=False,
                result=None,
                error="Gmail API 429 rate limited",
                retryable=True,
            )
        if resp.status_code >= 500:
            return ActionResult(
                ok=False,
                result=None,
                error=f"Gmail API {resp.status_code} Server Error",
                retryable=True,
            )
        if not resp.is_success:
            return ActionResult(
                ok=False,
                result=None,
                error=f"Gmail API error {resp.status_code}: {resp.text[:200]}",
                retryable=False,
            )

        # Confirmed send: capture the provider message id and mark the intent
        # 'sent' in its own transaction, before returning — so even if the
        # executor's terminal write then fails and the message redelivers, the
        # replay reads 'sent' and no-ops (that write is the dedup arbiter).
        provider_message_id = self._provider_message_id(resp)
        await self._dedup.mark_sent(key, provider_message_id)
        return self._ok_result(params, subject, provider_message_id)

    def _ok_result(
        self,
        params: EmailSendParams,
        subject: str,
        provider_message_id: str | None,
        *,
        deduped: bool = False,
    ) -> ActionResult:
        """Success envelope for a send. ``deduped=True`` marks a dedup no-op (ADR-070)."""
        result: dict[str, Any] = {
            "recipients": [str(addr) for addr in params.to],
            "subject": subject,
            "provider": "gmail",
            "provider_message_id": provider_message_id,
        }
        if deduped:
            result["deduped"] = True
        return ActionResult(ok=True, result=result, error=None, retryable=False)

    @staticmethod
    def _provider_message_id(resp: httpx.Response) -> str | None:
        """Extract the Gmail message id from a 2xx send response (best-effort)."""
        try:
            return resp.json().get("id")
        except (ValueError, AttributeError):
            return None

    async def _build_body(
        self,
        params: EmailSendParams,
        resolved_body: str | None,
        *,
        user_id: str,
    ) -> str | ActionResult:
        template = params.template or EmailTemplate.raw
        formatter = _TEMPLATE_FORMATTERS[template]

        if params.from_run_id is not None:
            return await self._body_from_upstream(params.from_run_id, formatter, user_id=user_id)

        if resolved_body is not None:
            return resolved_body

        return ActionResult(
            ok=False,
            result=None,
            error="Either body or from_run_id must be provided",
            retryable=False,
        )

    async def _body_from_upstream(
        self, from_run_id: int, formatter: Callable[..., str], *, user_id: str
    ) -> str:
        from app.db.engine import async_session_factory  # noqa: PLC0415

        async with async_session_factory() as session:
            async with session.begin():
                return await resolve_for_display(
                    from_run_id, session, formatter=formatter, user_id=user_id
                )


# ---------------------------------------------------------------------------
# Google token refresh helper
# ---------------------------------------------------------------------------


def _make_google_refresher():
    """Return an async TokenRefresher that refreshes a Google OAuth access token."""

    async def _refresher(token_data: dict) -> dict:
        refresh_token = token_data.get("refresh_token")
        if not refresh_token:
            return token_data

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                _GOOGLE_TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": settings.google_client_id,
                    "client_secret": settings.google_client_secret,
                },
            )
        resp.raise_for_status()
        new_data = resp.json()
        if "expires_in" in new_data:
            expires_at = datetime.now(UTC) + timedelta(seconds=int(new_data["expires_in"]))
            new_data["expires_at"] = expires_at.isoformat()
        # Preserve refresh_token if the new response omits it (Google sometimes does this).
        if "refresh_token" not in new_data and refresh_token:
            new_data["refresh_token"] = refresh_token
        return {**token_data, **new_data}

    return _refresher
