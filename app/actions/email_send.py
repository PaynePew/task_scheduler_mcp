"""email_send action handler — dual-mode email delivery (ADR-050, ADR-051).

Credential modes:
  - Public users (oauth_connection): sends via Gmail API using the caller's
    Google OAuth token via get_token(user_id, "google") (provider="google").
  - Operator (operator env): sends via SMTP using SMTP_HOST/PORT/USER/PASSWORD
    and EMAIL_FROM env vars (ADR-032). No Google connection required.

At run time the handler inspects ``run.user_id`` against
``settings.operator_user_id`` to select the path:
  - operator or operator_user_id unset → SMTP path
  - everyone else → Gmail API path

Chain-fed mode (ADR-033): set from_run_id to consume a prior handler's
JobRun.result as the email body. Supports templates (raw, digest_v1).

SMTP error classification:
    SMTP 5.x.x (permanent failure)  → retryable=False → DLQ
    SMTP 4.x.x (temporary failure)  → retryable=True
    Auth failure (535 / 5.7.0)      → retryable=False → DLQ + operator action
    TLS / connection / timeout      → retryable=True

Gmail API error classification:
    401 Unauthorized                → retryable=False (reconnect at /connections)
    403 Forbidden                   → retryable=False (insufficient scope)
    429 Rate limited                → retryable=True
    5xx Server Error                → retryable=True
    network / timeout               → retryable=True
"""

from __future__ import annotations

import base64
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from enum import StrEnum
from typing import Any, ClassVar

import aiosmtplib
import httpx
from pydantic import BaseModel, EmailStr

from app.actions.base import ActionResult, CredentialMode
from app.chain.upstream_reader import resolve_for_display
from app.config.settings import settings
from app.connections.store import ConnectionMiss, get_token
from app.secrets.resolver import SecretResolutionError, build_effective_whitelist, resolve

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
    """Sends email via Gmail API (public users) or SMTP (operator).

    Public users need a Google OAuth connection (provider="google") stored in
    the connection store. The operator uses SMTP env vars (ADR-032).

    Patch app.actions.email_send.get_token in tests to override OAuth lookup.
    """

    name: ClassVar[str] = "email_send"
    description: ClassVar[str] = (
        "Sends a transactional email. "
        "Public users: sends via Gmail using your connected Google account "
        "(connect at /connections). "
        "Operator: sends via SMTP using SMTP_HOST, SMTP_PORT, SMTP_USER, "
        "SMTP_PASSWORD, EMAIL_FROM env vars. "
        "Use from_run_id to chain from a prior handler's output. "
        "Templates: raw (default), digest_v1."
    )
    summary_line: ClassVar[str] = (
        "Sends an email via the user's Gmail (or operator SMTP); supports digest_v1 chaining."
    )
    params_model: ClassVar[type[BaseModel]] = EmailSendParams
    timeout_seconds: ClassVar[int] = 30
    requires_operator: ClassVar[bool] = False
    credential_mode: ClassVar[CredentialMode] = CredentialMode.oauth_connection
    required_provider: ClassVar[str | None] = "google"

    async def execute(self, run: Any, params: EmailSendParams) -> ActionResult:
        # Resolve any ${VAR} references (ADR-032) in subject/body.
        whitelist = build_effective_whitelist()
        env = dict(os.environ)
        try:
            resolved_subject = resolve(params.subject, env, whitelist)
            resolved_body = (
                resolve(params.body, env, whitelist) if params.body is not None else None
            )
        except SecretResolutionError as exc:
            return ActionResult(ok=False, result=None, error=str(exc), retryable=False)

        # Build email body (direct or chain-fed).
        body_text = await self._build_body(params, resolved_body)
        if isinstance(body_text, ActionResult):
            return body_text

        # Route to SMTP (operator / no operator configured) or Gmail (public user).
        user_id: str = run.user_id
        operator_uid = settings.operator_user_id
        if not operator_uid or user_id == operator_uid:
            return await self._send_via_smtp(params, resolved_subject, body_text)
        return await self._send_via_gmail(user_id, params, resolved_subject, body_text)

    async def _send_via_smtp(
        self,
        params: EmailSendParams,
        subject: str,
        body_text: str,
    ) -> ActionResult:
        smtp_host = os.environ.get("SMTP_HOST")
        smtp_port_raw = os.environ.get("SMTP_PORT", "587")
        smtp_user = os.environ.get("SMTP_USER") or None
        smtp_password = os.environ.get("SMTP_PASSWORD") or None
        email_from = os.environ.get("EMAIL_FROM")
        use_starttls = os.environ.get("SMTP_USE_STARTTLS", "true").lower() not in ("false", "0")

        if not smtp_host:
            return ActionResult(
                ok=False,
                result=None,
                error="SMTP_HOST environment variable is not set",
                retryable=False,
            )
        if not email_from:
            return ActionResult(
                ok=False,
                result=None,
                error="EMAIL_FROM environment variable is not set",
                retryable=False,
            )
        try:
            smtp_port = int(smtp_port_raw)
        except ValueError:
            return ActionResult(
                ok=False,
                result=None,
                error=f"SMTP_PORT must be an integer, got: {smtp_port_raw!r}",
                retryable=False,
            )

        msg = EmailMessage()
        msg["From"] = email_from
        msg["To"] = ", ".join(str(addr) for addr in params.to)
        msg["Subject"] = subject
        msg.set_content(body_text)

        try:
            await aiosmtplib.send(
                msg,
                hostname=smtp_host,
                port=smtp_port,
                username=smtp_user,
                password=smtp_password,
                start_tls=use_starttls,
                timeout=float(self.timeout_seconds),
            )
        except aiosmtplib.SMTPAuthenticationError as exc:
            return ActionResult(
                ok=False,
                result=None,
                error=f"SMTP auth failure (code {exc.code}): {exc.message}",
                retryable=False,
            )
        except aiosmtplib.SMTPRecipientsRefused as exc:
            codes = [r.code for r in exc.recipients]
            retryable = bool(codes) and all(400 <= c < 500 for c in codes)
            return ActionResult(
                ok=False,
                result=None,
                error=f"All recipients refused: {[r.recipient for r in exc.recipients]}",
                retryable=retryable,
            )
        except aiosmtplib.SMTPSenderRefused as exc:
            retryable = 400 <= exc.code < 500
            return ActionResult(
                ok=False,
                result=None,
                error=f"Sender refused (code {exc.code}): {exc.message}",
                retryable=retryable,
            )
        except aiosmtplib.SMTPDataError as exc:
            retryable = 400 <= exc.code < 500
            return ActionResult(
                ok=False,
                result=None,
                error=f"SMTP data error (code {exc.code}): {exc.message}",
                retryable=retryable,
            )
        except (
            aiosmtplib.SMTPConnectError,
            aiosmtplib.SMTPServerDisconnected,
            aiosmtplib.SMTPTimeoutError,
        ) as exc:
            return ActionResult(
                ok=False,
                result=None,
                error=f"SMTP connection/timeout error: {exc}",
                retryable=True,
            )
        except aiosmtplib.SMTPException as exc:
            code = getattr(exc, "code", None)
            retryable = True if code is None else (400 <= code < 500)
            return ActionResult(
                ok=False,
                result=None,
                error=f"SMTP error: {exc}",
                retryable=retryable,
            )

        return ActionResult(
            ok=True,
            result={
                "recipients": [str(addr) for addr in params.to],
                "subject": subject,
                "provider": "smtp",
            },
            error=None,
            retryable=False,
        )

    async def _send_via_gmail(
        self,
        user_id: str | None,
        params: EmailSendParams,
        subject: str,
        body_text: str,
    ) -> ActionResult:
        if not user_id:
            return ActionResult(
                ok=False,
                result=None,
                error="email_send: no user_id on run — cannot resolve Google connection",
                retryable=False,
            )

        try:
            token = await get_token(user_id, "google", refresher=_make_google_refresher())
        except ConnectionMiss:
            connect_url = f"{settings.connections_base_url}/connections"
            return ActionResult(
                ok=False,
                result=None,
                error=(
                    f"No Google connection for this user. "
                    f"Connect your Google account at {connect_url}"
                ),
                retryable=False,
            )

        # Build RFC 2822 message and encode as base64url for the Gmail API.
        msg = EmailMessage()
        msg["To"] = ", ".join(str(addr) for addr in params.to)
        msg["Subject"] = subject
        msg.set_content(body_text)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()

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

        return ActionResult(
            ok=True,
            result={
                "recipients": [str(addr) for addr in params.to],
                "subject": subject,
                "provider": "gmail",
            },
            error=None,
            retryable=False,
        )

    async def _build_body(
        self,
        params: EmailSendParams,
        resolved_body: str | None,
    ) -> str | ActionResult:
        template = params.template or EmailTemplate.raw
        formatter = _TEMPLATE_FORMATTERS[template]

        if params.from_run_id is not None:
            return await self._body_from_upstream(params.from_run_id, formatter)

        if resolved_body is not None:
            return resolved_body

        return ActionResult(
            ok=False,
            result=None,
            error="Either body or from_run_id must be provided",
            retryable=False,
        )

    async def _body_from_upstream(self, from_run_id: int, formatter: Callable[..., str]) -> str:
        from app.db.engine import async_session_factory  # noqa: PLC0415

        async with async_session_factory() as session:
            async with session.begin():
                return await resolve_for_display(from_run_id, session, formatter=formatter)


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
