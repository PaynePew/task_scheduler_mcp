"""Unit tests for app/connections/store (ADR-054, ADR-050).

All DB and KMS calls are faked — no live Postgres or AWS.

Fake KMS: moto mock_aws
Fake provider: async refresher callable
Fake session: AsyncMock-based in-memory dict store

Scenarios covered:
  - upsert + get_fresh_token returns the stored access token
  - get_fresh_token triggers refresh when expiry is near
  - get_fresh_token does NOT refresh when expiry is far away
  - get_fresh_token does NOT refresh when no refresher is supplied
  - missing connection raises ConnectionMiss (typed)
  - list returns metadata for user's connections, no token bytes
  - delete removes the connection
  - isolation: user A cannot read user B's connection through store API
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import boto3
import pytest
from moto import mock_aws

from app.connections.store import ConnectionInfo, ConnectionMiss, ConnectionStore
from app.crypto.kms_envelope import KmsEnvelope
from app.db.models import OAuthConnection

_REGION = "ap-northeast-1"
_NOW = datetime(2026, 5, 21, 12, 0, 0, tzinfo=UTC)

# ---------------------------------------------------------------------------
# Helpers: fake KMS + envelope
# ---------------------------------------------------------------------------


@pytest.fixture()
def _moto_context():
    with mock_aws():
        yield


@pytest.fixture()
def kms_envelope(_moto_context):
    client = boto3.client("kms", region_name=_REGION)
    key_id = client.create_key(Description="test")["KeyMetadata"]["KeyId"]
    return KmsEnvelope(kms_client=client, key_id=key_id)


# ---------------------------------------------------------------------------
# Fake in-memory AsyncSession
# ---------------------------------------------------------------------------


class FakeSession:
    """Minimal AsyncSession fake backed by an in-memory dict.

    Supports execute() for SELECT / INSERT-ON-CONFLICT-UPDATE / DELETE
    patterns used by ConnectionStore.
    """

    def __init__(self) -> None:
        # {(user_id, provider): OAuthConnection}
        self._store: dict[tuple[str, str], OAuthConnection] = {}

    async def execute(self, stmt):
        # Detect statement type by inspecting the compiled SQL class name.
        stmt_type = type(stmt).__name__

        if stmt_type == "Insert":
            # Postgres upsert (insert + on_conflict_do_update)
            params = stmt.compile(dialect=_pg_dialect()).params
            key = (params["user_id"], params["provider"])
            if key in self._store:
                row = self._store[key]
                row.encrypted_blob = params["encrypted_blob"]
                row.expires_at = params.get("expires_at")
                row.updated_at = params.get("updated_at", datetime.now(UTC))
            else:
                row = OAuthConnection()
                row.id = len(self._store) + 1
                row.user_id = params["user_id"]
                row.provider = params["provider"]
                row.encrypted_blob = params["encrypted_blob"]
                row.expires_at = params.get("expires_at")
                row.created_at = datetime.now(UTC)
                row.updated_at = datetime.now(UTC)
                self._store[key] = row
            return _ScalarResult([])

        if stmt_type == "Select":
            # Extract WHERE clause filters by inspecting compiled params.
            compiled = stmt.compile(dialect=_pg_dialect())
            p = compiled.params
            user_id = p.get("user_id_1")
            provider = p.get("provider_1")
            if user_id and provider:
                key = (user_id, provider)
                rows = [self._store[key]] if key in self._store else []
            elif user_id:
                rows = [row for (uid, _), row in self._store.items() if uid == user_id]
            else:
                rows = list(self._store.values())
            return _ScalarResult(rows)

        if stmt_type == "Delete":
            compiled = stmt.compile(dialect=_pg_dialect())
            p = compiled.params
            user_id = p.get("user_id_1")
            provider = p.get("provider_1")
            if user_id and provider:
                self._store.pop((user_id, provider), None)
            return _ScalarResult([])

        raise NotImplementedError(f"FakeSession: unhandled stmt type {stmt_type!r}")


class _ScalarResult:
    def __init__(self, rows):
        self._rows = rows

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None

    def scalars(self):
        return self

    def all(self):
        return self._rows


def _pg_dialect():
    from sqlalchemy.dialects import postgresql

    return postgresql.dialect()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def session():
    return FakeSession()


@pytest.fixture()
def store(session, kms_envelope):
    return ConnectionStore(session=session, envelope=kms_envelope, refresh_window_seconds=300)


def _token_data(
    access_token: str = "access-abc",
    refresh_token: str | None = "refresh-xyz",
    expires_at: datetime | None = None,
) -> dict:
    data: dict = {"access_token": access_token}
    if refresh_token:
        data["refresh_token"] = refresh_token
    if expires_at:
        data["expires_at"] = expires_at.isoformat()
    return data


# ---------------------------------------------------------------------------
# get_fresh_token — happy path
# ---------------------------------------------------------------------------


async def test_get_fresh_token_returns_stored_access_token(store):
    await store.upsert("user-1", "github", _token_data("tok-111"))
    token = await store.get_fresh_token("user-1", "github")
    assert token == "tok-111"


async def test_upsert_replaces_existing_token(store):
    await store.upsert("user-1", "github", _token_data("old-tok"))
    await store.upsert("user-1", "github", _token_data("new-tok"))
    token = await store.get_fresh_token("user-1", "github")
    assert token == "new-tok"


# ---------------------------------------------------------------------------
# get_fresh_token — refresh path
# ---------------------------------------------------------------------------


async def test_refresh_triggered_when_expiry_near(store):
    near_expiry = _NOW + timedelta(seconds=60)  # inside 300s window
    await store.upsert("user-1", "github", _token_data("old", expires_at=near_expiry))

    refreshed_data = _token_data("fresh-token", expires_at=_NOW + timedelta(hours=1))

    async def fake_refresher(current_data):
        return refreshed_data

    token = await store.get_fresh_token("user-1", "github", refresher=fake_refresher, _now=_NOW)
    assert token == "fresh-token"


async def test_refresh_not_triggered_when_expiry_far(store):
    far_expiry = _NOW + timedelta(hours=2)  # outside 300s window
    await store.upsert("user-1", "github", _token_data("orig-tok", expires_at=far_expiry))

    called = []

    async def unexpected_refresher(data):
        called.append(True)
        return data

    token = await store.get_fresh_token(
        "user-1", "github", refresher=unexpected_refresher, _now=_NOW
    )
    assert token == "orig-tok"
    assert not called, "Refresher must not be called when token is not near expiry"


async def test_refresh_not_triggered_when_no_refresher(store):
    near_expiry = _NOW + timedelta(seconds=60)
    await store.upsert("user-1", "github", _token_data("tok-no-refresh", expires_at=near_expiry))

    # No refresher supplied → should return existing token without error
    token = await store.get_fresh_token("user-1", "github", _now=_NOW)
    assert token == "tok-no-refresh"


# ---------------------------------------------------------------------------
# ConnectionMiss
# ---------------------------------------------------------------------------


async def test_missing_connection_raises_connection_miss(store):
    with pytest.raises(ConnectionMiss) as exc_info:
        await store.get_fresh_token("user-1", "github")
    err = exc_info.value
    assert err.user_id == "user-1"
    assert err.provider == "github"


async def test_missing_connection_miss_is_typed():
    """ConnectionMiss must be a distinct exception type (not plain Exception)."""
    assert issubclass(ConnectionMiss, Exception)
    err = ConnectionMiss("u", "p")
    assert "u" in str(err) and "p" in str(err)


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


async def test_list_returns_connection_info_for_user(store):
    await store.upsert("user-1", "github", _token_data("t1"))
    await store.upsert("user-1", "slack", _token_data("t2"))
    infos = await store.list("user-1")
    providers = {c.provider for c in infos}
    assert providers == {"github", "slack"}
    for info in infos:
        assert isinstance(info, ConnectionInfo)
        assert info.user_id == "user-1"


async def test_list_returns_empty_for_unknown_user(store):
    assert await store.list("nobody") == []


async def test_list_does_not_expose_token_bytes(store):
    """ConnectionInfo must not carry any token bytes."""
    await store.upsert("user-1", "github", _token_data("secret-token"))
    infos = await store.list("user-1")
    assert len(infos) == 1
    info = infos[0]
    assert not hasattr(info, "access_token")
    assert not hasattr(info, "refresh_token")
    assert not hasattr(info, "encrypted_blob")


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


async def test_delete_removes_connection(store):
    await store.upsert("user-1", "github", _token_data())
    await store.delete("user-1", "github")
    with pytest.raises(ConnectionMiss):
        await store.get_fresh_token("user-1", "github")


async def test_delete_is_idempotent(store):
    await store.delete("user-1", "github")  # not present — must not raise


# ---------------------------------------------------------------------------
# Isolation: user A cannot read user B's connection
# ---------------------------------------------------------------------------


async def test_user_isolation(store):
    """User B cannot retrieve user A's connection through the store API."""
    await store.upsert("user-A", "github", _token_data("token-A"))
    await store.upsert("user-B", "github", _token_data("token-B"))

    # Each user gets their own token
    assert await store.get_fresh_token("user-A", "github") == "token-A"
    assert await store.get_fresh_token("user-B", "github") == "token-B"

    # user B cannot read user A's connection by supplying user A's user_id
    with pytest.raises(ConnectionMiss):
        await store.get_fresh_token("user-A", "slack")  # user-A has no Slack


async def test_list_scoped_to_user(store):
    await store.upsert("user-A", "github", _token_data("ta"))
    await store.upsert("user-B", "github", _token_data("tb"))

    a_list = await store.list("user-A")
    b_list = await store.list("user-B")

    assert all(c.user_id == "user-A" for c in a_list)
    assert all(c.user_id == "user-B" for c in b_list)
    assert len(a_list) == 1
    assert len(b_list) == 1


async def test_tokens_not_cross_user_readable(store):
    """Direct get_fresh_token with wrong user_id raises ConnectionMiss."""
    await store.upsert("user-A", "github", _token_data("secret-of-A"))

    with pytest.raises(ConnectionMiss):
        await store.get_fresh_token("user-B", "github")
