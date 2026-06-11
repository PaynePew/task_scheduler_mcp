"""End-to-end OAuth onboarding walk for ADR-061 Layers 1+2+3 (issue #212, parent PRD #196).

Exercises the discovery → error → connect → retry loop across all three layers
in one ordered scenario so they cannot silently drift out of alignment:

  - Layer 1 (S1, #197 / PR #199) — handler protocol attributes are imported by
    the MCP server's `build_system_instruction` at module import; observed
    implicitly here via `_build_action_list` consuming the same registry.
  - Layer 2 (S2, #213) — `task.list_actions.v1` carries `auth_required`,
    `auth_status`, and `connect_url`.
  - Layer 3 (S3, #211 / PR #214) — `MISSING_CONNECTION` surfaces from
    `execute()` paths (via `check_oauth_for_execute`), not just preflight.

The phases are ordered: PHASE 6 only matters if PHASE 4 ran; PHASE 8 only if
PHASE 7 ran. A single scenario test enforces this; per-phase tests would
duplicate setup and miss the cross-slice contract (Layer 2's `not_connected`
↔ Layer 3's `MISSING_CONNECTION` error envelope).

Slack is the only provider exercised — sufficient to demonstrate the contract,
per the issue's "out of scope" note. No external network: Slack HTTP is faked.

Run with:
    uv run pytest -m integration tests/integration/test_oauth_full_walk.py
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import boto3
import httpx
import pytest
import pytest_asyncio
from moto import mock_aws
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.actions.registry import ACTION_REGISTRY
from app.connections.store import ConnectionStore
from app.crypto.kms_envelope import KmsEnvelope
from app.db.engine import create_async_engine
from app.db.models import JobRun, OAuthConnection
from app.mcp.handlers.status import handle_task_status
from app.mcp.server import _build_action_list, _handle_task_create, _load_user_connected_providers
from app.queue.sqs import SQSClient
from app.workers.executor import process_one

_REGION = "ap-northeast-1"
_USER = "user_full_walk_test"


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
            await session.execute(delete(OAuthConnection).where(OAuthConnection.user_id == _USER))
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


def _fake_slack_ok_ctx() -> tuple[MagicMock, list[dict]]:
    """Return (patched httpx.AsyncClient ctx, captured payloads list) — Slack returns ok:true."""
    captured: list[dict] = []

    async def fake_post(url: str, **kwargs: object) -> httpx.Response:
        captured.append(kwargs.get("json", {}))
        body = {"ok": True, "channel": "C12345", "ts": "1234567890.123456"}
        return httpx.Response(200, content=json.dumps(body).encode())

    mock_client = AsyncMock()
    mock_client.post = fake_post
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_client)
    ctx.__aexit__ = AsyncMock(return_value=None)
    return ctx, captured


async def _queue_and_dispatch(
    factory: async_sessionmaker,
    sqs: SQSClient,
    job_id: int,
) -> int:
    """Take the latest run for *job_id*, flip PENDING → QUEUED, dispatch via process_one.

    The full scheduler/dispatcher chain is out of scope for this scenario test;
    this helper bridges the gap between create_job (which inserts a PENDING run)
    and process_one (which expects QUEUED). Returns the dispatched run_id so the
    caller can assert on it.
    """
    async with factory() as session:
        async with session.begin():
            run = (
                await session.execute(
                    select(JobRun)
                    .where(JobRun.job_id == job_id)
                    .order_by(JobRun.run_id.desc())
                    .limit(1)
                )
            ).scalar_one()
            run.status = "QUEUED"
            run_id = run.run_id

    message = {
        "Body": json.dumps({"run_id": run_id, "job_id": job_id}),
        "ReceiptHandle": f"fake-receipt-{run_id}",
        "MessageId": f"fake-msg-{run_id}",
    }
    # Synthetic message — we never put it on ElasticMQ, so the fake receipt
    # cannot be deleted there. Swallow the delete to prevent ReceiptHandle errors.
    sqs.delete_message = lambda r: None
    await process_one(factory, sqs, message, registry=ACTION_REGISTRY)
    return run_id


async def _mark_connection_expired(
    factory: async_sessionmaker,
    provider: str,
) -> None:
    """Force the existing connection's expires_at into the past — simulates token expiry."""
    past = datetime.now(UTC) - timedelta(hours=1)
    async with factory() as session:
        async with session.begin():
            row = (
                await session.execute(
                    select(OAuthConnection).where(
                        OAuthConnection.user_id == _USER,
                        OAuthConnection.provider == provider,
                    )
                )
            ).scalar_one()
            row.expires_at = past


# ---------------------------------------------------------------------------
# Scenario test: full OAuth onboarding walk
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_oauth_full_walk(session_factory, sqs):
    """Discovery → error → connect → retry → expire → retry-fail → reconnect → retry-ok."""
    with mock_aws():
        kms_client = boto3.client("kms", region_name=_REGION)
        key_id = kms_client.create_key(Description="test-full-walk")["KeyMetadata"]["KeyId"]
        fake_envelope = KmsEnvelope(kms_client=kms_client, key_id=key_id)

        slack_ctx, slack_captured = _fake_slack_ok_ctx()

        # Single patch stack covers every KMS-envelope lookup site so all three
        # layers (preflight, list_actions, execute) see the same moto-backed
        # encryption. async_session_factory is also patched so deferred imports
        # inside the handlers (slack_post._message_from_upstream, _oauth)
        # use the per-test engine, not the module-cached one that may be bound
        # to a different event loop after a prior test ran.
        with (
            patch("app.mcp.server._make_server_kms_envelope", return_value=fake_envelope),
            patch("app.actions._oauth.kms_envelope_from_settings", return_value=fake_envelope),
            patch(
                "app.crypto.envelope_factory.kms_envelope_from_settings",
                return_value=fake_envelope,
            ),
            patch("app.db.engine.async_session_factory", session_factory),
            patch(
                "app.actions.slack_post.get_token",
                AsyncMock(return_value="xoxb-fake"),
            ),
            patch("app.actions.slack_post.httpx.AsyncClient", return_value=slack_ctx),
        ):
            # -----------------------------------------------------------------
            # PHASE 1 — fresh user with zero connections (DB cleaned by fixture).
            # -----------------------------------------------------------------

            # -----------------------------------------------------------------
            # PHASE 2 — Layer 2: list_actions reports auth_status=not_connected
            #            with a usable connect_url for slack_post.
            # -----------------------------------------------------------------
            connected = await _load_user_connected_providers(_USER, session_factory)
            actions = _build_action_list(connected)
            slack_meta = next(a for a in actions if a["name"] == "slack_post")
            assert slack_meta["auth_required"] is True
            assert slack_meta["auth_status"] == "not_connected"
            assert slack_meta["connect_url"] is not None
            assert "/connections" in slack_meta["connect_url"]

            # -----------------------------------------------------------------
            # PHASE 3 — preflight: task.create.v1 returns MISSING_CONNECTION envelope.
            # -----------------------------------------------------------------
            create_resp = await _handle_task_create(
                {
                    "action": "slack_post",
                    "action_params": {"channel": "#test", "message": "hi"},
                },
                _USER,
                session_factory=session_factory,
                _queue_depth=0,
            )
            assert create_resp["ok"] is False
            assert create_resp["error"]["code"] == "MISSING_CONNECTION"
            assert "/connections" in create_resp["error"]["message"]
            assert "/connections" in create_resp["error"].get("connect_url", "")

            # -----------------------------------------------------------------
            # PHASE 4 — user completes OAuth at /connections (no expiry).
            # -----------------------------------------------------------------
            async with session_factory() as session:
                async with session.begin():
                    store = ConnectionStore(session=session, envelope=fake_envelope)
                    await store.upsert(
                        _USER,
                        "slack",
                        {"access_token": "xoxb-fresh", "expires_at": None},
                    )

            # -----------------------------------------------------------------
            # PHASE 5 — Layer 2 now reports auth_status=connected.
            # -----------------------------------------------------------------
            connected = await _load_user_connected_providers(_USER, session_factory)
            actions = _build_action_list(connected)
            slack_meta = next(a for a in actions if a["name"] == "slack_post")
            assert slack_meta["auth_status"] == "connected"

            # -----------------------------------------------------------------
            # PHASE 6 — task.create succeeds; dispatch run; task.status → SUCCEEDED.
            # -----------------------------------------------------------------
            create_resp = await _handle_task_create(
                {
                    "action": "slack_post",
                    "action_params": {"channel": "#test", "message": "hi"},
                },
                _USER,
                session_factory=session_factory,
                _queue_depth=0,
            )
            assert create_resp["ok"] is True
            ok_job_id = create_resp["data"]["job_id"]
            await _queue_and_dispatch(session_factory, sqs, ok_job_id)

            status = await handle_task_status(
                {"job_id": ok_job_id},
                user_id=_USER,
                session_factory=session_factory,
            )
            assert status["ok"] is True
            assert status["data"]["status"] == "completed"
            assert status["data"].get("error") is None
            assert slack_captured, "Slack handler must have POSTed for the first run"

            # -----------------------------------------------------------------
            # PHASE 7 — token expires between runs.
            # -----------------------------------------------------------------
            await _mark_connection_expired(session_factory, "slack")

            # Layer 2 is presence-based: an expired-but-present connection still
            # reports 'connected' (expiry is recovered via refresh at execute time,
            # and this matches the presence-based /connections web dashboard). The
            # real expiry signal is enforced at execute() — see PHASE 8 below.
            connected = await _load_user_connected_providers(_USER, session_factory)
            assert "slack" in connected

            # -----------------------------------------------------------------
            # PHASE 8 — next dispatch surfaces MISSING_CONNECTION from execute (S3).
            #
            # Note: task.create preflight only checks "row exists & decryptable",
            # not "not expired" (the row IS present, refresh window is just stale),
            # so create succeeds. The expiry must be caught at execute() time —
            # this is precisely Layer 3's contract.
            # -----------------------------------------------------------------
            create_resp = await _handle_task_create(
                {
                    "action": "slack_post",
                    "action_params": {"channel": "#test", "message": "hi"},
                },
                _USER,
                session_factory=session_factory,
                _queue_depth=0,
            )
            assert create_resp["ok"] is True, "create.v1 should not block on expired row"
            expired_job_id = create_resp["data"]["job_id"]
            captured_before = len(slack_captured)
            await _queue_and_dispatch(session_factory, sqs, expired_job_id)

            # Slack HTTP must NOT have been called — check_oauth_for_execute
            # short-circuits before get_token / httpx.
            assert len(slack_captured) == captured_before, (
                "Slack HTTP must not be hit when connection has expired"
            )

            status = await handle_task_status(
                {"job_id": expired_job_id},
                user_id=_USER,
                session_factory=session_factory,
            )
            assert status["ok"] is True
            assert status["data"]["status"] == "failed"
            err = status["data"].get("error")
            assert err is not None, "task.status.v1 must surface error block on failure"
            assert err["code"] == "MISSING_CONNECTION"
            assert "/connections" in err.get("connect_url", "")

            # -----------------------------------------------------------------
            # PHASE 9 — user reconnects; next dispatch SUCCEEDED again.
            # -----------------------------------------------------------------
            async with session_factory() as session:
                async with session.begin():
                    store = ConnectionStore(session=session, envelope=fake_envelope)
                    await store.upsert(
                        _USER,
                        "slack",
                        {"access_token": "xoxb-reconnected", "expires_at": None},
                    )

            create_resp = await _handle_task_create(
                {
                    "action": "slack_post",
                    "action_params": {"channel": "#test", "message": "hi"},
                },
                _USER,
                session_factory=session_factory,
                _queue_depth=0,
            )
            assert create_resp["ok"] is True
            final_job_id = create_resp["data"]["job_id"]
            captured_before = len(slack_captured)
            await _queue_and_dispatch(session_factory, sqs, final_job_id)

            assert len(slack_captured) > captured_before, (
                "reconnect must allow the next run to POST"
            )
            final_status = await handle_task_status(
                {"job_id": final_job_id},
                user_id=_USER,
                session_factory=session_factory,
            )
            assert final_status["ok"] is True
            assert final_status["data"]["status"] == "completed"
            assert final_status["data"].get("error") is None
