"""Tests for Layer-2 auth metadata on task.list_actions.v1 (issue #210, ADR-061).

Covers:
  - auth_required is True exactly for oauth_connection handlers
  - connect_url is non-null iff auth_required
  - non-OAuth actions always return auth_status == "n/a"
  - user with Slack connection: slack_post "connected", github_digest "not_connected"
  - user with no connections: every OAuth action returns "not_connected"
  - batch-load: N actions do not produce N DB queries
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar
from unittest.mock import MagicMock, patch

import boto3
import pytest
import pytest_asyncio
from moto import mock_aws
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.actions.base import CredentialMode
from app.connections.store import ConnectionStore
from app.crypto.kms_envelope import KmsEnvelope
from app.db.engine import create_async_engine
from app.db.models import OAuthConnection
from app.mcp.server import _build_action_list, _load_user_connected_providers

_REGION = "ap-northeast-1"
_USER_ID = "user_test_list_actions_auth"


# ---------------------------------------------------------------------------
# Helpers: fake handler factory (unit tests)
# ---------------------------------------------------------------------------


def _make_handler(
    name: str,
    credential_mode: CredentialMode = CredentialMode.none,
    required_provider: str | None = None,
) -> Any:
    class _Base:
        description: ClassVar[str] = "stub"
        timeout_seconds: ClassVar[int] = 30

        class params_model:
            @staticmethod
            def model_json_schema() -> dict:
                return {}

    return type(
        f"H_{name}",
        (_Base,),
        {
            "name": name,
            "summary_line": "stub summary",
            "credential_mode": credential_mode,
            "required_provider": required_provider,
        },
    )()


# ---------------------------------------------------------------------------
# Unit tests — no DB, no KMS, just the pure _build_action_list() function
# ---------------------------------------------------------------------------


def test_auth_required_true_for_oauth_connection_handlers():
    handler = _make_handler("slack_x", CredentialMode.oauth_connection, "slack")
    with patch("app.mcp.server.ACTION_REGISTRY", {"slack_x": handler}):
        actions = _build_action_list(set())
    assert len(actions) == 1
    assert actions[0]["auth_required"] is True


def test_auth_required_false_for_non_oauth_handlers():
    for mode in [CredentialMode.none, CredentialMode.operator_env]:
        handler = _make_handler("plain", mode, None)
        with patch("app.mcp.server.ACTION_REGISTRY", {"plain": handler}):
            actions = _build_action_list(set())
        assert actions[0]["auth_required"] is False


def test_connect_url_non_null_iff_auth_required():
    oauth_handler = _make_handler("oauth_a", CredentialMode.oauth_connection, "slack")
    plain_handler = _make_handler("plain_b", CredentialMode.none, None)
    with patch(
        "app.mcp.server.ACTION_REGISTRY",
        {"oauth_a": oauth_handler, "plain_b": plain_handler},
    ):
        actions = _build_action_list(set())

    by_name = {a["name"]: a for a in actions}
    assert by_name["oauth_a"]["connect_url"] is not None
    assert by_name["plain_b"]["connect_url"] is None


def test_non_oauth_action_always_returns_na():
    handler = _make_handler("echo_x", CredentialMode.none, None)
    with patch("app.mcp.server.ACTION_REGISTRY", {"echo_x": handler}):
        actions = _build_action_list(connected_providers={"slack", "github"})
    assert actions[0]["auth_status"] == "n/a"


def test_connected_provider_gives_connected_status():
    handler = _make_handler("slack_x", CredentialMode.oauth_connection, "slack")
    with patch("app.mcp.server.ACTION_REGISTRY", {"slack_x": handler}):
        actions = _build_action_list(connected_providers={"slack"})
    assert actions[0]["auth_status"] == "connected"


def test_missing_provider_gives_not_connected_status():
    handler = _make_handler("github_x", CredentialMode.oauth_connection, "github")
    with patch("app.mcp.server.ACTION_REGISTRY", {"github_x": handler}):
        actions = _build_action_list(connected_providers={"slack"})  # github absent
    assert actions[0]["auth_status"] == "not_connected"


def test_no_connected_providers_every_oauth_not_connected():
    handlers = {
        "a": _make_handler("a", CredentialMode.oauth_connection, "slack"),
        "b": _make_handler("b", CredentialMode.oauth_connection, "github"),
        "c": _make_handler("c", CredentialMode.none, None),
    }
    with patch("app.mcp.server.ACTION_REGISTRY", handlers):
        actions = _build_action_list(connected_providers=set())
    by_name = {a["name"]: a for a in actions}
    assert by_name["a"]["auth_status"] == "not_connected"
    assert by_name["b"]["auth_status"] == "not_connected"
    assert by_name["c"]["auth_status"] == "n/a"


def test_build_action_list_existing_fields_preserved():
    """The three legacy fields must still appear alongside the new auth fields."""
    handler = _make_handler("echo_x", CredentialMode.none, None)
    with patch("app.mcp.server.ACTION_REGISTRY", {"echo_x": handler}):
        actions = _build_action_list()
    assert "name" in actions[0]
    assert "description" in actions[0]
    assert "timeout_seconds" in actions[0]
    assert "params_schema" in actions[0]


# ---------------------------------------------------------------------------
# Integration tests — real DB, moto KMS
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    async with factory() as session:
        async with session.begin():
            await session.execute(
                delete(OAuthConnection).where(OAuthConnection.user_id == _USER_ID)
            )
    await engine.dispose()


@pytest.fixture
def fake_envelope():
    with mock_aws():
        kms_client = boto3.client("kms", region_name=_REGION)
        key_id = kms_client.create_key(Description="test")["KeyMetadata"]["KeyId"]
        yield KmsEnvelope(kms_client=kms_client, key_id=key_id)


@pytest.mark.integration
async def test_user_with_slack_connection_connected_github_not_connected(
    session_factory, fake_envelope
):
    """User has Slack connected: slack_post → connected; github_digest → not_connected."""
    async with session_factory() as session:
        async with session.begin():
            store = ConnectionStore(session, fake_envelope)
            await store.upsert(
                _USER_ID,
                "slack",
                {"access_token": "xoxb-test", "expires_at": None},
            )

    with patch("app.mcp.server._make_server_kms_envelope", return_value=fake_envelope):
        connected = await _load_user_connected_providers(_USER_ID, session_factory)

    slack_handler = MagicMock()
    slack_handler.name = "slack_post"
    slack_handler.description = "Post a Slack message"
    slack_handler.timeout_seconds = 30
    slack_handler.credential_mode = CredentialMode.oauth_connection
    slack_handler.required_provider = "slack"
    slack_handler.params_model.model_json_schema.return_value = {}

    github_handler = MagicMock()
    github_handler.name = "github_digest"
    github_handler.description = "GitHub digest"
    github_handler.timeout_seconds = 30
    github_handler.credential_mode = CredentialMode.oauth_connection
    github_handler.required_provider = "github"
    github_handler.params_model.model_json_schema.return_value = {}

    with patch(
        "app.mcp.server.ACTION_REGISTRY",
        {"slack_post": slack_handler, "github_digest": github_handler},
    ):
        actions = _build_action_list(connected)

    by_name = {a["name"]: a for a in actions}
    assert by_name["slack_post"]["auth_status"] == "connected"
    assert by_name["github_digest"]["auth_status"] == "not_connected"


@pytest.mark.integration
async def test_user_with_no_connections_all_oauth_not_connected(session_factory, fake_envelope):
    """User has no connections: every OAuth action returns not_connected."""
    with patch("app.mcp.server._make_server_kms_envelope", return_value=fake_envelope):
        connected = await _load_user_connected_providers(_USER_ID, session_factory)

    assert connected == set()

    slack_handler = MagicMock()
    slack_handler.name = "slack_post"
    slack_handler.description = "stub"
    slack_handler.timeout_seconds = 30
    slack_handler.credential_mode = CredentialMode.oauth_connection
    slack_handler.required_provider = "slack"
    slack_handler.params_model.model_json_schema.return_value = {}

    with patch("app.mcp.server.ACTION_REGISTRY", {"slack_post": slack_handler}):
        actions = _build_action_list(connected)

    assert actions[0]["auth_status"] == "not_connected"


@pytest.mark.integration
async def test_batch_load_single_query_for_multiple_oauth_actions(session_factory, fake_envelope):
    """_load_user_connected_providers issues one DB query regardless of action count."""
    async with session_factory() as session:
        async with session.begin():
            store = ConnectionStore(session, fake_envelope)
            await store.upsert(_USER_ID, "slack", {"access_token": "tok"})
            await store.upsert(_USER_ID, "github", {"access_token": "tok"})

    call_count = 0
    real_list = ConnectionStore.list

    async def counting_list(self, uid):
        nonlocal call_count
        call_count += 1
        return await real_list(self, uid)

    with (
        patch("app.mcp.server._make_server_kms_envelope", return_value=fake_envelope),
        patch.object(ConnectionStore, "list", counting_list),
    ):
        await _load_user_connected_providers(_USER_ID, session_factory)

    assert call_count == 1, f"Expected 1 DB query, got {call_count}"


@pytest.mark.integration
async def test_expired_connection_treated_as_not_connected(session_factory, fake_envelope):
    """A connection whose expires_at is in the past should not appear in connected set."""
    past = datetime.now(UTC) - timedelta(hours=1)
    async with session_factory() as session:
        async with session.begin():
            store = ConnectionStore(session, fake_envelope)
            await store.upsert(
                _USER_ID, "slack", {"access_token": "old", "expires_at": past.isoformat()}
            )

    with patch("app.mcp.server._make_server_kms_envelope", return_value=fake_envelope):
        connected = await _load_user_connected_providers(_USER_ID, session_factory)

    assert "slack" not in connected
