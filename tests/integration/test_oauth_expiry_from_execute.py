"""Integration tests for Layer 3: surface MISSING_CONNECTION from execute() paths.

Verifies that OAuth-gated handlers return the canonical MISSING_CONNECTION
error code when:
  - the user has no connection at execute time (row absent)
  - the connection was valid at create time but expired by execute time
  - a token refresh attempt fails (bad refresh token / provider outage)
  - a token refresh succeeds (token updated, action proceeds)

Tests use moto KMS for the expiry/refresh scenarios; the missing-row case
only needs Postgres (ConnectionMiss is raised before any decryption).

Run with:
    uv run pytest -m integration tests/integration/test_oauth_expiry_from_execute.py
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

from app.actions._oauth import check_oauth_for_execute
from app.actions.base import ActionResult
from app.connections.store import ConnectionStore
from app.crypto.kms_envelope import KmsEnvelope
from app.db.engine import create_async_engine
from app.db.models import Job, JobRun
from app.mcp.handlers.status import handle_task_status
from app.queue.sqs import SQSClient
from app.workers.executor import process_one

_REGION = "ap-northeast-1"
_USER = "oauth-expiry-test-user"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def session_factory():
    """Fresh engine per test; cleans all job + connection data on teardown."""
    engine = create_async_engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    async with factory() as session:
        async with session.begin():
            await session.execute(text("DELETE FROM run_events"))
            await session.execute(text("DELETE FROM job_runs"))
            await session.execute(text("DELETE FROM jobs"))
            await session.execute(
                text("DELETE FROM oauth_connections WHERE user_id = :uid"),
                {"uid": _USER},
            )
    await engine.dispose()


@pytest.fixture
def sqs() -> SQSClient:
    """SQSClient pointed at ElasticMQ; drains leftover messages."""
    client = SQSClient()
    while True:
        msgs = client.receive_messages(max_messages=10, wait_seconds=0)
        if not msgs:
            break
        for msg in msgs:
            client.delete_message(msg["ReceiptHandle"])
    return client


async def _insert_slack_run(
    factory: async_sessionmaker,
    *,
    user_id: str = _USER,
) -> tuple[Job, JobRun]:
    """Insert a slack_post Job + QUEUED JobRun."""
    scheduled = datetime.now(tz=UTC) - timedelta(seconds=10)
    async with factory() as session:
        async with session.begin():
            job = Job(
                user_id=user_id,
                description="oauth expiry test",
                action="slack_post",
                action_params={"channel": "#test", "message": "hello"},
                job_type="one_shot",
                scheduled_at=scheduled,
            )
            session.add(job)
            await session.flush()
            bucket = scheduled.replace(minute=0, second=0, microsecond=0).isoformat()
            run = JobRun(
                time_bucket=bucket,
                job_id=job.job_id,
                user_id=user_id,
                scheduled_at=scheduled,
                status="QUEUED",
            )
            session.add(run)
    return job, run


def _make_sqs_message(run_id: int, job_id: int) -> dict:
    return {
        "Body": json.dumps({"run_id": run_id, "job_id": job_id}),
        "ReceiptHandle": f"fake-receipt-{run_id}",
        "MessageId": f"fake-msg-{run_id}",
    }


# ---------------------------------------------------------------------------
# Integration: missing connection at execute time
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_missing_connection_at_execute_marks_run_failed(session_factory, sqs):
    """No connection row at all → FAILED with error_code=MISSING_CONNECTION.

    check_oauth_for_execute skips (no KMS in this env), then get_token raises
    ConnectionMiss → SlackPostHandler's except block returns the canonical error.
    """
    from app.actions.registry import ACTION_REGISTRY

    job, run = await _insert_slack_run(session_factory)
    message = _make_sqs_message(run.run_id, job.job_id)

    deleted_receipts: list[str] = []
    sqs.delete_message = deleted_receipts.append

    # Patch the module-level async_session_factory so handler code that lazily
    # imports it (e.g. get_token) uses the per-test engine, not the cached
    # module-level one that may carry stale connections from a prior test's loop.
    with patch("app.db.engine.async_session_factory", session_factory):
        await process_one(session_factory, sqs, message, registry=ACTION_REGISTRY)

    async with session_factory() as session:
        async with session.begin():
            updated_run = (
                await session.execute(select(JobRun).where(JobRun.run_id == run.run_id))
            ).scalar_one()

    assert updated_run.status == "FAILED"
    assert updated_run.error_code == "MISSING_CONNECTION"
    assert updated_run.error_message is not None
    assert "/connections" in updated_run.error_message
    assert message["ReceiptHandle"] in deleted_receipts


# ---------------------------------------------------------------------------
# Integration: expired connection at execute time (requires moto KMS)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_expired_connection_at_execute_marks_run_failed(session_factory, sqs):
    """Connection exists but expires_at is in the past → FAILED MISSING_CONNECTION.

    Uses moto KMS to simulate a real encrypted connection row.  The helper
    detects expiry via the plaintext expires_at column (no decryption needed)
    and returns MISSING_CONNECTION immediately (no refresher for slack).
    """
    with mock_aws():
        kms_client = boto3.client("kms", region_name=_REGION)
        key_id = kms_client.create_key(Description="test-expiry")["KeyMetadata"]["KeyId"]
        fake_envelope = KmsEnvelope(kms_client=kms_client, key_id=key_id)

        # Seed a connection whose token expired an hour ago.
        async with session_factory() as session:
            async with session.begin():
                store = ConnectionStore(session=session, envelope=fake_envelope)
                await store.upsert(
                    _USER,
                    "slack",
                    {
                        "access_token": "xoxb-expired-token",
                        "expires_at": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
                    },
                )

        job, run = await _insert_slack_run(session_factory)
        message = _make_sqs_message(run.run_id, job.job_id)

        deleted_receipts: list[str] = []
        sqs.delete_message = deleted_receipts.append

        from app.actions.registry import ACTION_REGISTRY

        # Keep mock_aws active AND patch the helper's kms_envelope_from_settings so
        # check_oauth_for_execute uses the moto envelope (not None).
        # Patch both the KMS envelope (so the check runs) and the module-level
        # async_session_factory (so the helper uses the per-test engine, not the
        # cached module-level one that may be bound to a different event loop).
        with (
            patch(
                "app.actions._oauth.kms_envelope_from_settings",
                return_value=fake_envelope,
            ),
            patch("app.db.engine.async_session_factory", session_factory),
        ):
            await process_one(session_factory, sqs, message, registry=ACTION_REGISTRY)

    async with session_factory() as session:
        async with session.begin():
            updated_run = (
                await session.execute(select(JobRun).where(JobRun.run_id == run.run_id))
            ).scalar_one()

    assert updated_run.status == "FAILED"
    assert updated_run.error_code == "MISSING_CONNECTION"
    assert updated_run.error_message is not None
    assert "/connections" in updated_run.error_message
    assert message["ReceiptHandle"] in deleted_receipts


# ---------------------------------------------------------------------------
# Integration: task.status.v1 exposes MISSING_CONNECTION code
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_status_exposes_missing_connection_code(session_factory, sqs):
    """After a MISSING_CONNECTION failure, task.status.v1 carries code in response.

    Uses the missing-row scenario (no KMS needed) to trigger the failure, then
    asserts that task.status.v1 returns the canonical error envelope.
    """
    from app.actions.registry import ACTION_REGISTRY

    job, run = await _insert_slack_run(session_factory)
    message = _make_sqs_message(run.run_id, job.job_id)
    sqs.delete_message = lambda r: None

    # Run without a connection → FAILED MISSING_CONNECTION.
    with patch("app.db.engine.async_session_factory", session_factory):
        await process_one(session_factory, sqs, message, registry=ACTION_REGISTRY)

    result = await handle_task_status(
        {"job_id": job.job_id},
        user_id=_USER,
        session_factory=session_factory,
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "failed"
    err = result["data"].get("error")
    assert err is not None, "error block must be present in status response"
    assert err["code"] == "MISSING_CONNECTION"
    assert "message" in err
    assert "/connections" in err.get("connect_url", "")


# ---------------------------------------------------------------------------
# Unit: refresh-succeeds path (tests helper directly with moto KMS)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_refresh_succeeds_returns_none(session_factory):
    """Expired connection + working refresher → check_oauth_for_execute returns None."""
    with mock_aws():
        kms_client = boto3.client("kms", region_name=_REGION)
        key_id = kms_client.create_key(Description="test-refresh-ok")["KeyMetadata"]["KeyId"]
        fake_envelope = KmsEnvelope(kms_client=kms_client, key_id=key_id)

        expired_at = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        async with session_factory() as session:
            async with session.begin():
                store = ConnectionStore(session=session, envelope=fake_envelope)
                await store.upsert(
                    _USER,
                    "google",
                    {
                        "access_token": "stale-token",
                        "refresh_token": "valid-refresh",
                        "expires_at": expired_at,
                    },
                )

        new_expiry = (datetime.now(UTC) + timedelta(hours=1)).isoformat()

        async def fake_refresher(token_data: dict) -> dict:
            return {**token_data, "access_token": "fresh-token", "expires_at": new_expiry}

        with patch(
            "app.actions._oauth.kms_envelope_from_settings",
            return_value=fake_envelope,
        ):
            result = await check_oauth_for_execute(
                _USER,
                "google",
                refresher=fake_refresher,
                _session_factory=session_factory,
            )

    assert result is None, "refresh succeeded — check should return None"


# ---------------------------------------------------------------------------
# Unit: refresh-fails path (tests helper directly with moto KMS)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_refresh_fails_returns_missing_connection(session_factory):
    """Expired connection + failing refresher → returns MISSING_CONNECTION."""
    with mock_aws():
        kms_client = boto3.client("kms", region_name=_REGION)
        key_id = kms_client.create_key(Description="test-refresh-fail")["KeyMetadata"]["KeyId"]
        fake_envelope = KmsEnvelope(kms_client=kms_client, key_id=key_id)

        expired_at = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        async with session_factory() as session:
            async with session.begin():
                store = ConnectionStore(session=session, envelope=fake_envelope)
                await store.upsert(
                    _USER,
                    "google",
                    {
                        "access_token": "stale-token",
                        "refresh_token": "revoked-refresh",
                        "expires_at": expired_at,
                    },
                )

        async def failing_refresher(token_data: dict) -> dict:
            raise RuntimeError("refresh token revoked")

        with patch(
            "app.actions._oauth.kms_envelope_from_settings",
            return_value=fake_envelope,
        ):
            result = await check_oauth_for_execute(
                _USER,
                "google",
                refresher=failing_refresher,
                _session_factory=session_factory,
            )

    assert isinstance(result, ActionResult)
    assert result.error_code == "MISSING_CONNECTION"
    assert result.ok is False
    assert result.retryable is False
    assert "/connections" in (result.error or "")
