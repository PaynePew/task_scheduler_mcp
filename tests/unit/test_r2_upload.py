"""Unit tests for the r2_upload action handler (no real network or DB)."""

from __future__ import annotations

import json
import ssl
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import botocore.exceptions
import pytest

from app.actions.r2_upload import _MULTIPART_THRESHOLD, R2UploadHandler, R2UploadParams
from app.actions.registry import ACTION_REGISTRY
from app.chain.upstream_reader import InvalidJson, NoResult, Ok, UpstreamError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_R2_ENV = {
    "R2_ACCOUNT_ID": "test-account",
    "R2_ACCESS_KEY_ID": "test-key",
    "R2_SECRET_ACCESS_KEY": "test-secret",
    "R2_BUCKET": "test-bucket",
}

_HEAD_RESPONSE = {
    "ETag": '"abc123"',
    "ContentLength": 11,
}


def _make_mock_s3_client(
    head_response: dict | None = None,
    upload_side_effect: Exception | None = None,
) -> MagicMock:
    """Return a mock boto3 S3 client."""
    client = MagicMock()
    if upload_side_effect is not None:
        client.upload_fileobj.side_effect = upload_side_effect
    else:
        client.upload_fileobj.return_value = None
    client.head_object.return_value = head_response or _HEAD_RESPONSE
    return client


def _make_handler(
    mock_client: MagicMock | None = None,
    session_factory: Any = None,
) -> R2UploadHandler:
    factory = (lambda: mock_client) if mock_client is not None else None
    return R2UploadHandler(session_factory=session_factory, s3_client_factory=factory)


def _make_mock_session_factory() -> Any:
    """Build an async-context-manager mock with the shape the handler expects.

    The actual upstream variant returned by ``read_upstream`` is configured per
    test via ``patch("app.actions.r2_upload.read_upstream", ...)``; this helper
    only fakes the ``async with factory() as session: async with session.begin():``
    plumbing the handler walks before calling read_upstream.
    """
    mock_session = AsyncMock()
    mock_session.begin = MagicMock(return_value=mock_session)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    mock_factory_ctx = AsyncMock()
    mock_factory_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_factory_ctx.__aexit__ = AsyncMock(return_value=None)

    return MagicMock(return_value=mock_factory_ctx)


# ---------------------------------------------------------------------------
# Happy path — small upload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_small_upload_ok():
    """Content under 100 MB is uploaded and returns bucket/key/etag/size_bytes."""
    mock_client = _make_mock_s3_client(head_response={"ETag": '"deadbeef"', "ContentLength": 5})
    handler = _make_handler(mock_client)
    params = R2UploadParams(bucket_path="reports/small.txt", content="hello")

    with patch.dict("os.environ", _R2_ENV):
        result = await handler.execute(run=None, params=params)

    assert result.ok is True
    assert result.error is None
    assert result.result is not None
    assert result.result["bucket"] == "test-bucket"
    assert result.result["key"] == "reports/small.txt"
    assert result.result["etag"] == "deadbeef"
    assert result.result["size_bytes"] == 5

    mock_client.upload_fileobj.assert_called_once()
    mock_client.head_object.assert_called_once_with(Bucket="test-bucket", Key="reports/small.txt")


@pytest.mark.asyncio
async def test_small_upload_passes_content_type():
    mock_client = _make_mock_s3_client()
    handler = _make_handler(mock_client)
    params = R2UploadParams(
        bucket_path="reports/data.json",
        content='{"key": "value"}',
        content_type="application/json",
    )

    with patch.dict("os.environ", _R2_ENV):
        await handler.execute(run=None, params=params)

    _, _, call_kwargs = mock_client.upload_fileobj.mock_calls[0]
    extra_args = call_kwargs.get("ExtraArgs", {})
    assert extra_args.get("ContentType") == "application/json"


# ---------------------------------------------------------------------------
# Happy path — large (>100 MB) multipart upload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_large_upload_uses_multipart_threshold():
    """Content over 100 MB is uploaded; TransferConfig with correct threshold is passed."""
    large_content = "x" * (_MULTIPART_THRESHOLD + 1)
    head_resp = {"ETag": '"multipart-etag-3"', "ContentLength": len(large_content)}
    mock_client = _make_mock_s3_client(head_response=head_resp)
    handler = _make_handler(mock_client)
    params = R2UploadParams(bucket_path="dumps/large.bin", content=large_content)

    with patch.dict("os.environ", _R2_ENV):
        result = await handler.execute(run=None, params=params)

    assert result.ok is True
    assert result.result["size_bytes"] == len(large_content)

    _, _, call_kwargs = mock_client.upload_fileobj.mock_calls[0]
    config = call_kwargs.get("Config")
    assert config is not None
    assert config.multipart_threshold == _MULTIPART_THRESHOLD


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------


def _make_client_error(status_code: int, code: str = "Error") -> botocore.exceptions.ClientError:
    return botocore.exceptions.ClientError(
        {
            "Error": {"Code": code, "Message": "error"},
            "ResponseMetadata": {"HTTPStatusCode": status_code},
        },
        "upload_fileobj",
    )


@pytest.mark.asyncio
async def test_4xx_auth_error_not_retryable():
    """401 auth error → DLQ (not retryable)."""
    mock_client = _make_mock_s3_client(
        upload_side_effect=_make_client_error(401, "InvalidAccessKeyId")
    )
    handler = _make_handler(mock_client)
    params = R2UploadParams(bucket_path="test.txt", content="data")

    with patch.dict("os.environ", _R2_ENV):
        result = await handler.execute(run=None, params=params)

    assert result.ok is False
    assert result.retryable is False


@pytest.mark.asyncio
async def test_4xx_bad_request_not_retryable():
    """400 bad request → DLQ (not retryable)."""
    mock_client = _make_mock_s3_client(
        upload_side_effect=_make_client_error(400, "InvalidBucketName")
    )
    handler = _make_handler(mock_client)
    params = R2UploadParams(bucket_path="test.txt", content="data")

    with patch.dict("os.environ", _R2_ENV):
        result = await handler.execute(run=None, params=params)

    assert result.ok is False
    assert result.retryable is False


@pytest.mark.asyncio
async def test_4xx_forbidden_not_retryable():
    """403 forbidden → DLQ (not retryable)."""
    mock_client = _make_mock_s3_client(upload_side_effect=_make_client_error(403, "AccessDenied"))
    handler = _make_handler(mock_client)
    params = R2UploadParams(bucket_path="test.txt", content="data")

    with patch.dict("os.environ", _R2_ENV):
        result = await handler.execute(run=None, params=params)

    assert result.ok is False
    assert result.retryable is False


@pytest.mark.asyncio
async def test_5xx_server_error_is_retryable():
    """500 server error → retry."""
    mock_client = _make_mock_s3_client(upload_side_effect=_make_client_error(500, "InternalError"))
    handler = _make_handler(mock_client)
    params = R2UploadParams(bucket_path="test.txt", content="data")

    with patch.dict("os.environ", _R2_ENV):
        result = await handler.execute(run=None, params=params)

    assert result.ok is False
    assert result.retryable is True


@pytest.mark.asyncio
async def test_ssl_error_is_retryable():
    """SSL error → retry."""
    ssl_exc = ssl.SSLError("certificate verify failed")
    mock_client = _make_mock_s3_client(upload_side_effect=ssl_exc)
    handler = _make_handler(mock_client)
    params = R2UploadParams(bucket_path="test.txt", content="data")

    with patch.dict("os.environ", _R2_ENV):
        result = await handler.execute(run=None, params=params)

    assert result.ok is False
    assert result.retryable is True
    assert "SSL" in (result.error or "")


@pytest.mark.asyncio
async def test_network_error_is_retryable():
    """Network OSError → retry."""
    mock_client = _make_mock_s3_client(upload_side_effect=OSError("connection refused"))
    handler = _make_handler(mock_client)
    params = R2UploadParams(bucket_path="test.txt", content="data")

    with patch.dict("os.environ", _R2_ENV):
        result = await handler.execute(run=None, params=params)

    assert result.ok is False
    assert result.retryable is True


@pytest.mark.asyncio
async def test_botocore_error_is_retryable():
    """Generic BotoCoreError (e.g., endpoint connection error) → retry."""
    mock_client = _make_mock_s3_client(
        upload_side_effect=botocore.exceptions.EndpointConnectionError(
            endpoint_url="https://example.com"
        )
    )
    handler = _make_handler(mock_client)
    params = R2UploadParams(bucket_path="test.txt", content="data")

    with patch.dict("os.environ", _R2_ENV):
        result = await handler.execute(run=None, params=params)

    assert result.ok is False
    assert result.retryable is True


# ---------------------------------------------------------------------------
# from_run_id — upstream JSON → uploaded content
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_from_run_id_ok_serializes_json():
    """from_run_id with Ok upstream → JSON-serialized content is uploaded."""
    data = {"report": "daily", "count": 42}
    mock_session_factory = _make_mock_session_factory()
    mock_client = _make_mock_s3_client()
    handler = R2UploadHandler(
        session_factory=mock_session_factory, s3_client_factory=lambda: mock_client
    )
    params = R2UploadParams(bucket_path="reports/out.json", from_run_id=99)

    with patch("app.actions.r2_upload.read_upstream", AsyncMock(return_value=Ok(data=data))):
        with patch.dict("os.environ", _R2_ENV):
            result = await handler.execute(run=None, params=params)

    assert result.ok is True
    _, call_args, _ = mock_client.upload_fileobj.mock_calls[0]
    uploaded_bytes = call_args[0].read()
    assert json.loads(uploaded_bytes) == data


@pytest.mark.asyncio
async def test_from_run_id_upstream_error_returns_failure():
    """from_run_id with UpstreamError → handler returns not-retryable failure."""
    mock_session_factory = _make_mock_session_factory()
    mock_client = _make_mock_s3_client()
    handler = R2UploadHandler(
        session_factory=mock_session_factory, s3_client_factory=lambda: mock_client
    )
    params = R2UploadParams(bucket_path="reports/out.json", from_run_id=99)

    upstream_payload = UpstreamError(error_msg="upstream failed")
    with patch("app.actions.r2_upload.read_upstream", AsyncMock(return_value=upstream_payload)):
        with patch.dict("os.environ", _R2_ENV):
            result = await handler.execute(run=None, params=params)

    assert result.ok is False
    assert result.retryable is False
    assert "upstream failed" in (result.error or "")


@pytest.mark.asyncio
async def test_from_run_id_no_result_returns_failure():
    """from_run_id with NoResult → handler returns not-retryable failure."""
    mock_session_factory = _make_mock_session_factory()
    mock_client = _make_mock_s3_client()
    handler = R2UploadHandler(
        session_factory=mock_session_factory, s3_client_factory=lambda: mock_client
    )
    params = R2UploadParams(bucket_path="reports/out.json", from_run_id=99)

    with patch("app.actions.r2_upload.read_upstream", AsyncMock(return_value=NoResult())):
        with patch.dict("os.environ", _R2_ENV):
            result = await handler.execute(run=None, params=params)

    assert result.ok is False
    assert result.retryable is False


@pytest.mark.asyncio
async def test_from_run_id_invalid_json_uploads_raw():
    """from_run_id with InvalidJson → raw bytes are uploaded (pass-through)."""
    raw = "not-valid-json-but-upload-anyway"
    mock_session_factory = _make_mock_session_factory()
    mock_client = _make_mock_s3_client()
    handler = R2UploadHandler(
        session_factory=mock_session_factory, s3_client_factory=lambda: mock_client
    )
    params = R2UploadParams(bucket_path="reports/raw.txt", from_run_id=99)

    with patch("app.actions.r2_upload.read_upstream", AsyncMock(return_value=InvalidJson(raw=raw))):
        with patch.dict("os.environ", _R2_ENV):
            result = await handler.execute(run=None, params=params)

    assert result.ok is True
    _, call_args, _ = mock_client.upload_fileobj.mock_calls[0]
    uploaded_bytes = call_args[0].read()
    assert uploaded_bytes == raw.encode()


# ---------------------------------------------------------------------------
# Idempotency: same bucket_path + same content uploaded twice → second call succeeds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idempotency_same_path_and_content():
    """Uploading the same content to the same path twice → both calls succeed (R2 overwrite)."""
    mock_client = _make_mock_s3_client(head_response={"ETag": '"abc"', "ContentLength": 4})
    handler = _make_handler(mock_client)
    params = R2UploadParams(bucket_path="reports/idempotent.txt", content="same")

    with patch.dict("os.environ", _R2_ENV):
        result1 = await handler.execute(run=None, params=params)
        result2 = await handler.execute(run=None, params=params)

    assert result1.ok is True
    assert result2.ok is True
    assert result1.result == result2.result
    assert mock_client.upload_fileobj.call_count == 2


# ---------------------------------------------------------------------------
# Missing env vars
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_r2_env_vars_not_retryable():
    """Missing R2 credentials → not-retryable failure."""
    handler = R2UploadHandler()
    params = R2UploadParams(bucket_path="test.txt", content="data")

    with patch.dict("os.environ", {}, clear=True):
        result = await handler.execute(run=None, params=params)

    assert result.ok is False
    assert result.retryable is False
    assert "R2_ACCOUNT_ID" in (result.error or "")


@pytest.mark.asyncio
async def test_no_content_and_no_from_run_id_fails():
    """Neither content nor from_run_id provided → not-retryable failure."""
    mock_client = _make_mock_s3_client()
    handler = _make_handler(mock_client)
    params = R2UploadParams(bucket_path="test.txt")

    with patch.dict("os.environ", _R2_ENV):
        result = await handler.execute(run=None, params=params)

    assert result.ok is False
    assert result.retryable is False
    assert "content" in (result.error or "").lower() or "from_run_id" in (result.error or "")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_r2_upload_removed_from_registry():
    """r2_upload was demoted to a cron/shell script in issue #132 (ADR-051)."""
    assert "r2_upload" not in ACTION_REGISTRY


def test_registry_has_eight_actions():
    """task.list_actions.v1 exposes 8 actions (r2_upload removed #132; llm actions added #137)."""
    assert len(ACTION_REGISTRY) == 8
    expected = {
        "echo",
        "http_call",
        "calendar_digest_ics",
        "slack_post",
        "github_digest",
        "email_send",
        "llm_summarize",
        "llm_polish",
    }
    assert set(ACTION_REGISTRY.keys()) == expected
