"""r2_upload action handler — uploads content to Cloudflare R2 (S3-compatible storage).

Secrets convention (ADR-032): R2 credentials are read from environment variables
(R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET). Never stored
in action_params.

Chain-fed mode (ADR-033): set from_run_id to serialize an upstream handler's
JobRun.result JSON as the upload content. Enables workflows like:
    "schedule daily report → r2_upload → email link"

Multipart threshold: 100 MB (boto3 TransferConfig). Below threshold = single PUT;
above = automatic multipart. Idempotency: same bucket_path + same content = R2
overwrite (semantically a no-op); caller is responsible for content-hash naming.

Error classification:
    4xx (auth, bad request) → DLQ (not retryable)
    5xx → retry
    SSL / network / timeout → retry

Manual smoke test::

    R2_ACCOUNT_ID=<id> R2_ACCESS_KEY_ID=<key> R2_SECRET_ACCESS_KEY=<secret> \\
    R2_BUCKET=<bucket> uv run python -c "
    import asyncio
    from app.actions.r2_upload import R2UploadHandler, R2UploadParams
    p = R2UploadParams(bucket_path='test/smoke.txt', content='hello from r2_upload')
    r = asyncio.run(R2UploadHandler().execute(run=None, params=p))
    print(r)"
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import ssl
from typing import Any, ClassVar

import boto3
import botocore.exceptions
from boto3.s3.transfer import TransferConfig
from pydantic import BaseModel

from app.actions.base import ActionResult
from app.chain.upstream_reader import InvalidJson, NoResult, Ok, UpstreamError, read_upstream
from app.secrets.resolver import SecretResolutionError, build_effective_whitelist, resolve

_MULTIPART_THRESHOLD = 100 * 1024 * 1024  # 100 MB


class R2UploadParams(BaseModel):
    bucket_path: str
    content: str | None = None
    from_run_id: int | None = None
    content_type: str = "application/octet-stream"


def _make_s3_client(account_id: str, access_key_id: str, secret_access_key: str) -> Any:
    endpoint_url = f"https://{account_id}.r2.cloudflarestorage.com"
    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        region_name="auto",
    )


def _upload_sync(
    client: Any,
    bucket: str,
    key: str,
    content_bytes: bytes,
    content_type: str,
) -> dict[str, Any]:
    """Perform the S3 upload synchronously (run in thread pool via asyncio.to_thread).

    Uses TransferConfig with 100 MB multipart threshold. Returns {bucket, key, etag, size_bytes}.
    """
    config = TransferConfig(multipart_threshold=_MULTIPART_THRESHOLD)
    file_obj = io.BytesIO(content_bytes)
    client.upload_fileobj(
        file_obj,
        bucket,
        key,
        ExtraArgs={"ContentType": content_type},
        Config=config,
    )
    head = client.head_object(Bucket=bucket, Key=key)
    etag = head.get("ETag", "").strip('"')
    size_bytes = head.get("ContentLength", len(content_bytes))
    return {"bucket": bucket, "key": key, "etag": etag, "size_bytes": size_bytes}


class R2UploadHandler:
    """Uploads content to Cloudflare R2 via the S3-compatible API.

    Pass *session_factory* to override the DB session factory (useful in tests).
    Pass *s3_client_factory* to override S3 client creation (useful in tests).
    """

    name: ClassVar[str] = "r2_upload"
    description: ClassVar[str] = (
        "Uploads content to Cloudflare R2 (S3-compatible). "
        "Set R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET in env. "
        "Use from_run_id to chain from a prior handler's output. "
        "Returns {bucket, key, etag, size_bytes}."
    )
    params_model: ClassVar[type[BaseModel]] = R2UploadParams
    timeout_seconds: ClassVar[int] = 300

    def __init__(
        self,
        session_factory: Any = None,
        s3_client_factory: Any = None,
    ) -> None:
        self._session_factory = session_factory
        self._s3_client_factory = s3_client_factory

    def _get_session_factory(self) -> Any:
        if self._session_factory is not None:
            return self._session_factory
        from app.db.engine import async_session_factory

        return async_session_factory

    def _get_s3_client(self, account_id: str, access_key_id: str, secret_access_key: str) -> Any:
        if self._s3_client_factory is not None:
            return self._s3_client_factory()
        return _make_s3_client(account_id, access_key_id, secret_access_key)

    async def execute(self, run: Any, params: R2UploadParams) -> ActionResult:
        whitelist = build_effective_whitelist()
        env = dict(os.environ)
        try:
            resolved_bucket_path = resolve(params.bucket_path, env, whitelist)
            resolved_content = (
                resolve(params.content, env, whitelist) if params.content is not None else None
            )
        except SecretResolutionError as exc:
            return ActionResult(ok=False, result=None, error=str(exc), retryable=False)

        required = {
            "R2_ACCOUNT_ID": os.environ.get("R2_ACCOUNT_ID"),
            "R2_ACCESS_KEY_ID": os.environ.get("R2_ACCESS_KEY_ID"),
            "R2_SECRET_ACCESS_KEY": os.environ.get("R2_SECRET_ACCESS_KEY"),
            "R2_BUCKET": os.environ.get("R2_BUCKET"),
        }
        missing = [k for k, v in required.items() if v is None]
        if missing:
            return ActionResult(
                ok=False,
                result=None,
                error=f"Missing required environment variables: {', '.join(missing)}",
                retryable=False,
            )

        account_id = required["R2_ACCOUNT_ID"]
        access_key_id = required["R2_ACCESS_KEY_ID"]
        secret_access_key = required["R2_SECRET_ACCESS_KEY"]
        bucket = required["R2_BUCKET"]

        content_bytes_result = await self._resolve_content(params, resolved_content)
        if isinstance(content_bytes_result, ActionResult):
            return content_bytes_result
        content_bytes = content_bytes_result

        client = self._get_s3_client(account_id, access_key_id, secret_access_key)
        return await self._upload(
            client, bucket, resolved_bucket_path, content_bytes, params.content_type
        )

    async def _resolve_content(
        self, params: R2UploadParams, resolved_content: str | None
    ) -> bytes | ActionResult:
        if params.from_run_id is not None:
            factory = self._get_session_factory()
            async with factory() as session:
                async with session.begin():
                    upstream = await read_upstream(run_id=params.from_run_id, session=session)

            if isinstance(upstream, Ok):
                return json.dumps(upstream.data).encode()
            if isinstance(upstream, UpstreamError):
                return ActionResult(
                    ok=False,
                    result=None,
                    error=f"Upstream run failed: {upstream.error_msg}",
                    retryable=False,
                )
            if isinstance(upstream, NoResult):
                return ActionResult(
                    ok=False,
                    result=None,
                    error="Upstream run not found or has no result",
                    retryable=False,
                )
            if isinstance(upstream, InvalidJson):
                return upstream.raw.encode()
            return ActionResult(
                ok=False, result=None, error="Unknown upstream payload", retryable=False
            )

        if resolved_content is not None:
            return resolved_content.encode()

        return ActionResult(
            ok=False,
            result=None,
            error="Either content or from_run_id must be provided",
            retryable=False,
        )

    async def _upload(
        self,
        client: Any,
        bucket: str,
        key: str,
        content_bytes: bytes,
        content_type: str,
    ) -> ActionResult:
        try:
            result = await asyncio.to_thread(
                _upload_sync, client, bucket, key, content_bytes, content_type
            )
            return ActionResult(ok=True, result=result, error=None, retryable=False)
        except botocore.exceptions.ClientError as exc:
            status_code = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
            error_msg = str(exc)
            retryable = status_code >= 500
            return ActionResult(ok=False, result=None, error=error_msg, retryable=retryable)
        except botocore.exceptions.BotoCoreError as exc:
            return ActionResult(ok=False, result=None, error=str(exc), retryable=True)
        except ssl.SSLError as exc:
            return ActionResult(ok=False, result=None, error=f"SSL error: {exc}", retryable=True)
        except OSError as exc:
            return ActionResult(
                ok=False, result=None, error=f"Network error: {exc}", retryable=True
            )
