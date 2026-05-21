"""Unit tests for app.connections.store.get_token.

All DB and KMS calls are faked — no live Postgres or AWS.

Scenarios:
  - happy path: returns the stored access token string
  - ConnectionMiss propagation: raises ConnectionMiss when connection absent
  - refresher invocation: refresher is called when token is near expiry
  - inject-overrides: fake session_factory + envelope are used (no real DB / KMS)
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
import pytest
from moto import mock_aws
from sqlalchemy.dialects import postgresql

from app.connections.store import ConnectionMiss, ConnectionStore, get_token
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
# Minimal in-memory AsyncSession (mirrors test_connection_store pattern)
# ---------------------------------------------------------------------------


class _ScalarResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def scalar_one_or_none(self) -> Any:
        return self._rows[0] if self._rows else None

    def scalars(self) -> _ScalarResult:
        return self

    def all(self) -> list:
        return self._rows


def _pg_dialect():
    return postgresql.dialect()


class FakeSession:
    """Minimal AsyncSession backed by an in-memory dict."""

    def __init__(self) -> None:
        self._store: dict[tuple[str, str], OAuthConnection] = {}

    async def execute(self, stmt: Any) -> _ScalarResult:
        stmt_type = type(stmt).__name__

        if stmt_type == "Insert":
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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def session() -> FakeSession:
    return FakeSession()


def make_session_factory(session: FakeSession):
    """Wrap a FakeSession in an async context manager factory."""

    @asynccontextmanager
    async def _factory():
        yield session

    return _factory


def _token_data(
    access_token: str = "access-abc",
    expires_at: datetime | None = None,
) -> dict:
    data: dict = {"access_token": access_token}
    if expires_at:
        data["expires_at"] = expires_at.isoformat()
    return data


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_get_token_returns_access_token(session, kms_envelope):
    """Returns the access token string for a stored connection."""
    store = ConnectionStore(session=session, envelope=kms_envelope)
    await store.upsert("user-1", "github", _token_data("tok-abc"))

    async def refresher(data: dict) -> dict:
        return data

    token = await get_token(
        "user-1",
        "github",
        refresher,
        session_factory=make_session_factory(session),
        envelope=kms_envelope,
    )
    assert token == "tok-abc"


# ---------------------------------------------------------------------------
# ConnectionMiss propagation
# ---------------------------------------------------------------------------


async def test_get_token_connection_miss_propagates(session, kms_envelope):
    """Raises ConnectionMiss when no connection exists for (user_id, provider)."""

    async def refresher(data: dict) -> dict:
        return data

    with pytest.raises(ConnectionMiss) as exc_info:
        await get_token(
            "nobody",
            "github",
            refresher,
            session_factory=make_session_factory(session),
            envelope=kms_envelope,
        )
    err = exc_info.value
    assert err.user_id == "nobody"
    assert err.provider == "github"


# ---------------------------------------------------------------------------
# Refresher invocation
# ---------------------------------------------------------------------------


async def test_get_token_refresher_invoked_when_near_expiry(session, kms_envelope):
    """Refresher is called and new token is returned when token is near expiry."""
    # Token already expired — definitely inside the 300s refresh window.
    already_expired = datetime.now(UTC) - timedelta(hours=1)
    store = ConnectionStore(session=session, envelope=kms_envelope)
    await store.upsert(
        "user-1",
        "github",
        _token_data("old-tok", expires_at=already_expired),
    )

    refresher_called: list[bool] = []

    async def refresher(data: dict) -> dict:
        refresher_called.append(True)
        return {**data, "access_token": "new-tok"}

    token = await get_token(
        "user-1",
        "github",
        refresher,
        session_factory=make_session_factory(session),
        envelope=kms_envelope,
    )
    assert token == "new-tok"
    assert refresher_called, "refresher must be invoked for an expired token"


# ---------------------------------------------------------------------------
# Inject-overrides (fake session_factory + envelope)
# ---------------------------------------------------------------------------


async def test_get_token_uses_injected_session_factory_and_envelope(session, kms_envelope):
    """Injected session_factory and envelope are used — no real DB or KMS."""
    store = ConnectionStore(session=session, envelope=kms_envelope)
    await store.upsert("inject-user", "slack", _token_data("tok-inject"))

    async def refresher(data: dict) -> dict:
        return data

    token = await get_token(
        "inject-user",
        "slack",
        refresher,
        session_factory=make_session_factory(session),
        envelope=kms_envelope,
    )
    assert token == "tok-inject"


async def test_get_token_different_providers_isolated(session, kms_envelope):
    """Connections keyed by provider are isolated — no cross-provider reads."""
    store = ConnectionStore(session=session, envelope=kms_envelope)
    await store.upsert("user-1", "github", _token_data("github-tok"))
    await store.upsert("user-1", "slack", _token_data("slack-tok"))

    async def refresher(data: dict) -> dict:
        return data

    gh = await get_token(
        "user-1",
        "github",
        refresher,
        session_factory=make_session_factory(session),
        envelope=kms_envelope,
    )
    sl = await get_token(
        "user-1",
        "slack",
        refresher,
        session_factory=make_session_factory(session),
        envelope=kms_envelope,
    )
    assert gh == "github-tok"
    assert sl == "slack-tok"
