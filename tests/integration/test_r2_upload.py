"""Integration tests for r2_upload handler using moto S3 mock.

Tests the full upload stack with a real boto3 client (intercepted by moto):
  - small upload: object exists in bucket, ETag matches, size_bytes correct
  - from_run_id: upstream JSON from real Postgres row is serialized and uploaded
  - idempotency: same path + same content uploaded twice, second call succeeds

Run with:
    uv run pytest -m integration tests/integration/test_r2_upload.py
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import boto3
import pytest
import pytest_asyncio
from moto import mock_aws
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.actions.r2_upload import R2UploadHandler, R2UploadParams
from app.db.engine import create_async_engine
from app.db.models import Job, JobRun

# Dummy credentials accepted by moto (non-functional for real R2)
_R2_ENV = {
    "R2_ACCOUNT_ID": "test-account-id",
    "R2_ACCESS_KEY_ID": "test-access-key",
    "R2_SECRET_ACCESS_KEY": "test-secret-key",
    "R2_BUCKET": "test-bucket",
}

_BUCKET = "test-bucket"
_REGION = "us-east-1"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def session_factory():
    """Fresh async engine per test; cleans job data on teardown."""
    engine = create_async_engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    async with factory() as session:
        async with session.begin():
            await session.execute(text("DELETE FROM run_events"))
            await session.execute(text("DELETE FROM job_runs"))
            await session.execute(text("DELETE FROM jobs"))
    await engine.dispose()


async def _insert_run(
    factory: async_sessionmaker,
    result: str | None = None,
) -> JobRun:
    """Insert Job + JobRun with the given result; return the committed JobRun."""
    scheduled = datetime.now(tz=UTC) - timedelta(seconds=10)
    async with factory() as session:
        async with session.begin():
            job = Job(
                user_id="r2-upload-test",
                description="upstream job for r2_upload integration test",
                action="echo",
                action_params={"message": "upstream"},
                job_type="one_shot",
                scheduled_at=scheduled,
            )
            session.add(job)
            await session.flush()

            bucket = scheduled.replace(minute=0, second=0, microsecond=0).isoformat()
            run = JobRun(
                time_bucket=bucket,
                job_id=job.job_id,
                scheduled_at=scheduled,
                status="SUCCEEDED",
                result=result,
            )
            session.add(run)
            await session.flush()
            run_id = run.run_id

    async with factory() as session:
        async with session.begin():
            fetched_run = (
                await session.execute(select(JobRun).where(JobRun.run_id == run_id))
            ).scalar_one()
    return fetched_run


def _s3_client_factory_for_moto() -> object:
    """Create a boto3 S3 client configured for moto (us-east-1 standard endpoint)."""
    return boto3.client("s3", region_name=_REGION)


# ---------------------------------------------------------------------------
# Integration tests: moto S3 mock
# ---------------------------------------------------------------------------


@pytest.mark.integration
@mock_aws
def test_small_upload_etag_and_size(anyio_backend="asyncio"):
    """Sync-style test: small upload succeeds; ETag + size_bytes verified against moto."""
    import asyncio

    s3 = boto3.client("s3", region_name=_REGION)
    s3.create_bucket(Bucket=_BUCKET)

    content = "hello from r2_upload integration test"

    handler = R2UploadHandler(s3_client_factory=_s3_client_factory_for_moto)
    params = R2UploadParams(
        bucket_path="test/small.txt", content=content, content_type="text/plain"
    )

    with patch.dict("os.environ", _R2_ENV):
        result = asyncio.run(handler.execute(run=None, params=params))

    assert result.ok is True
    assert result.result is not None
    assert result.result["bucket"] == _BUCKET
    assert result.result["key"] == "test/small.txt"
    assert result.result["size_bytes"] == len(content.encode())
    assert result.result["etag"] != ""

    # Verify object actually exists in moto
    head = s3.head_object(Bucket=_BUCKET, Key="test/small.txt")
    assert head["ContentLength"] == len(content.encode())
    assert head["ContentType"] == "text/plain"


@pytest.mark.integration
@mock_aws
def test_idempotency_same_path_same_content():
    """Uploading the same content twice to the same path → both succeed (R2 overwrite)."""
    import asyncio

    s3 = boto3.client("s3", region_name=_REGION)
    s3.create_bucket(Bucket=_BUCKET)

    content = "idempotent content"
    handler = R2UploadHandler(s3_client_factory=_s3_client_factory_for_moto)
    params = R2UploadParams(bucket_path="idempotent/file.txt", content=content)

    with patch.dict("os.environ", _R2_ENV):
        r1 = asyncio.run(handler.execute(run=None, params=params))
        r2 = asyncio.run(handler.execute(run=None, params=params))

    assert r1.ok is True
    assert r2.ok is True
    # Both results should refer to the same object
    assert r1.result["key"] == r2.result["key"]
    assert r1.result["bucket"] == r2.result["bucket"]


@pytest.mark.integration
async def test_from_run_id_uploads_upstream_json(session_factory):
    """from_run_id reads upstream JobRun.result from Postgres and uploads as JSON bytes."""
    data = {"report": "nightly", "rows": 1234}
    upstream_run = await _insert_run(session_factory, result=json.dumps(data))

    with mock_aws():
        s3 = boto3.client("s3", region_name=_REGION)
        s3.create_bucket(Bucket=_BUCKET)

        handler = R2UploadHandler(
            session_factory=session_factory,
            s3_client_factory=_s3_client_factory_for_moto,
        )
        params = R2UploadParams(
            bucket_path="reports/upstream.json", from_run_id=upstream_run.run_id
        )

        with patch.dict("os.environ", _R2_ENV):
            result = await handler.execute(run=None, params=params)

        assert result.ok is True

        # Download the object and verify content matches upstream JSON
        obj = s3.get_object(Bucket=_BUCKET, Key="reports/upstream.json")
        uploaded = json.loads(obj["Body"].read())
        assert uploaded == data

        # Verify ETag and size
        head = s3.head_object(Bucket=_BUCKET, Key="reports/upstream.json")
        assert head["ContentLength"] == result.result["size_bytes"]
        assert result.result["etag"] != ""
