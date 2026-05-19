"""email_send action handler — sends transactional email via SMTP.

Secrets convention (ADR-032): SMTP credentials (SMTP_HOST, SMTP_PORT,
SMTP_USER, SMTP_PASSWORD, EMAIL_FROM) are read from environment variables.
They are NEVER stored in action_params.

Chain-fed mode (ADR-033): set from_run_id to consume a prior handler's
JobRun.result as the email body. Supports templates (raw, digest_v1).

Error classification:
    SMTP 5.x.x (permanent failure)  → retryable=False → DLQ
    SMTP 4.x.x (temporary failure)  → retryable=True
    Auth failure (535 / 5.7.0)      → retryable=False → DLQ + operator action
    TLS / connection / timeout      → retryable=True

SMTP transport uses STARTTLS by default (port 587). Set
SMTP_USE_STARTTLS=false in env to disable (e.g. for plain-text test servers).

Manual smoke test::

    SMTP_HOST=smtp.gmail.com SMTP_PORT=587 SMTP_USER=you@gmail.com \\
    SMTP_PASSWORD=<app-password> EMAIL_FROM=you@gmail.com \\
    uv run python -c "
    import asyncio
    from app.actions.email_send import EmailSendHandler, EmailSendParams
    p = EmailSendParams(to=['you@gmail.com'], subject='smoke test', body='hello from email_send')
    r = asyncio.run(EmailSendHandler().execute(run=None, params=p))
    print(r)"
"""

from __future__ import annotations

import os
from collections.abc import Callable
from email.message import EmailMessage
from enum import StrEnum
from typing import Any, ClassVar

import aiosmtplib
from pydantic import BaseModel, EmailStr

from app.actions.base import ActionResult
from app.chain.upstream_reader import (
    InvalidJson,
    NoResult,
    Ok,
    UpstreamError,
    read_upstream,
)
from app.secrets.resolver import SecretResolutionError, build_effective_whitelist, resolve


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
    """Sends a transactional email via SMTP (STARTTLS by default).

    Pass *session_factory* to override the default DB session factory — useful
    in tests that need to inject a pre-seeded session without touching the real
    engine pool.
    """

    name: ClassVar[str] = "email_send"
    description: ClassVar[str] = (
        "Sends a transactional email via SMTP. "
        "Set SMTP_HOST, SMTP_PORT (default 587), SMTP_USER, SMTP_PASSWORD, "
        "EMAIL_FROM in the server environment. "
        "Use from_run_id to chain from a prior handler's output. "
        "Templates: raw (default), digest_v1."
    )
    params_model: ClassVar[type[BaseModel]] = EmailSendParams
    timeout_seconds: ClassVar[int] = 30

    def __init__(self, session_factory: Any = None) -> None:
        self._session_factory = session_factory

    def _get_session_factory(self) -> Any:
        if self._session_factory is not None:
            return self._session_factory
        from app.db.engine import async_session_factory

        return async_session_factory

    async def execute(self, run: Any, params: EmailSendParams) -> ActionResult:
        # Resolve any ${VAR} references (ADR-032).
        whitelist = build_effective_whitelist()
        env = dict(os.environ)
        try:
            resolved_subject = resolve(params.subject, env, whitelist)
            resolved_body = (
                resolve(params.body, env, whitelist) if params.body is not None else None
            )
        except SecretResolutionError as exc:
            return ActionResult(ok=False, result=None, error=str(exc), retryable=False)

        # Read SMTP config from environment — never from action_params.
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

        # Build email body (direct or chain-fed).
        body_text = await self._build_body(params, resolved_body)
        if isinstance(body_text, ActionResult):
            return body_text

        # Assemble MIME message.
        msg = EmailMessage()
        msg["From"] = email_from
        msg["To"] = ", ".join(str(addr) for addr in params.to)
        msg["Subject"] = resolved_subject
        msg.set_content(body_text)

        # Send via aiosmtplib.
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
            # retryable only when ALL refused codes are 4xx (temporary).
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
            # Catch-all: classify by code if available, else retryable.
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
                "subject": resolved_subject,
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

    async def _body_from_upstream(
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
