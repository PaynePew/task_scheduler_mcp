"""http_call action handler — makes an HTTP request via httpx.AsyncClient."""

from __future__ import annotations

import os
from typing import Any, ClassVar

import httpx
from pydantic import BaseModel

from app.actions.base import ActionResult, CredentialMode
from app.secrets.resolver import SecretResolutionError, build_effective_whitelist, resolve

MAX_RESPONSE_BYTES = 2048  # 2 KB


class HttpCallParams(BaseModel):
    method: str
    url: str
    headers: dict[str, str] | None = None
    body: dict | str | None = None


class HttpCallHandler:
    name: ClassVar[str] = "http_call"
    description: ClassVar[str] = (
        "Makes an HTTP request to a URL. Supports any method (GET, POST, PUT, DELETE, PATCH). "
        "Response body is truncated to 2 KB. Server errors (5xx) are retried automatically; "
        "client errors (4xx) are permanent failures."
    )
    params_model: ClassVar[type[BaseModel]] = HttpCallParams
    timeout_seconds: ClassVar[int] = 30
    requires_operator: ClassVar[bool] = True
    credential_mode: ClassVar[CredentialMode] = CredentialMode.operator_env

    async def execute(self, run: Any, params: HttpCallParams) -> ActionResult:
        whitelist = build_effective_whitelist()
        env = dict(os.environ)
        try:
            resolved_headers = resolve(params.headers, env, whitelist)
            resolved_url = resolve(params.url, env, whitelist)
            resolved_body = resolve(params.body, env, whitelist)
        except SecretResolutionError as exc:
            return ActionResult(ok=False, result=None, error=str(exc), retryable=False)

        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                kwargs: dict[str, Any] = {
                    "method": params.method,
                    "url": resolved_url,
                    "headers": resolved_headers or {},
                }
                if isinstance(resolved_body, dict):
                    kwargs["json"] = resolved_body
                elif isinstance(resolved_body, str):
                    kwargs["content"] = resolved_body.encode("utf-8")

                response = await client.request(**kwargs)
        except httpx.RequestError as exc:
            return ActionResult(ok=False, result=None, error=str(exc), retryable=True)

        raw = response.content[:MAX_RESPONSE_BYTES]
        body_text = raw.decode("utf-8", errors="replace")

        retryable = response.status_code >= 500
        ok = response.is_success

        if ok:
            return ActionResult(
                ok=True,
                result={"status_code": response.status_code, "body": body_text},
                error=None,
                retryable=False,
            )
        return ActionResult(
            ok=False,
            result=None,
            error=f"HTTP {response.status_code}",
            retryable=retryable,
        )
