"""Connection store: per-user OAuth token storage (ADR-054, ADR-050).

Keyed by (user_id, provider); tokens are persisted as KMS envelope-encrypted
blobs — no plaintext bytes in the DB row (see KmsEnvelope).

Operations:
  upsert           — store or replace the token data for (user_id, provider)
  get_fresh_token  — return a valid access token, refreshing transparently if near expiry
  delete           — remove the connection entirely
  list             — enumerate all connections for a user_id (no token data exposed)

Provider-specific OAuth flow code is out of scope; callers supply a TokenRefresher
for the refresh path.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.crypto.kms_envelope import KmsEnvelope
from app.db.models import OAuthConnection

# ---------------------------------------------------------------------------
# Public data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConnectionInfo:
    """Provider connection metadata returned by list() — no token bytes."""

    user_id: str
    provider: str
    expires_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ConnectionMiss(Exception):
    """Raised when no connection exists for the requested (user_id, provider)."""

    def __init__(self, user_id: str, provider: str) -> None:
        self.user_id = user_id
        self.provider = provider
        super().__init__(f"No connection for user={user_id!r} provider={provider!r}")


# A TokenRefresher is an async callable that receives the current token payload
# dict and returns an updated one after performing the provider's refresh flow.
TokenRefresher = Callable[[dict[str, Any]], Coroutine[Any, Any, dict[str, Any]]]


# ---------------------------------------------------------------------------
# ConnectionStore
# ---------------------------------------------------------------------------


class ConnectionStore:
    """Stores and retrieves OAuth token data for (user_id, provider) pairs.

    Args:
        session: SQLAlchemy async session (caller owns the transaction).
        envelope: KmsEnvelope used to encrypt/decrypt token blobs.
        refresh_window_seconds: Re-fetch the access token when it expires
            within this many seconds (default: 300).
    """

    def __init__(
        self,
        session: AsyncSession,
        envelope: KmsEnvelope,
        refresh_window_seconds: int = 300,
    ) -> None:
        self._session = session
        self._envelope = envelope
        self._refresh_window = timedelta(seconds=refresh_window_seconds)

    # ------------------------------------------------------------------
    # upsert
    # ------------------------------------------------------------------

    async def upsert(
        self,
        user_id: str,
        provider: str,
        token_data: dict[str, Any],
    ) -> None:
        """Insert or replace the connection for (user_id, provider).

        *token_data* must contain at least ``access_token``.  If it contains
        ``expires_at`` (ISO-8601 string or datetime) that value is stored
        plaintext alongside the blob for cheap refresh-window queries.
        """
        blob = self._envelope.encrypt(json.dumps(token_data).encode())
        expires_at = _parse_expires_at(token_data.get("expires_at"))

        stmt = (
            pg_insert(OAuthConnection)
            .values(
                user_id=user_id,
                provider=provider,
                encrypted_blob=blob,
                expires_at=expires_at,
            )
            .on_conflict_do_update(
                constraint="uq_oauth_connections_user_provider",
                set_={
                    "encrypted_blob": blob,
                    "expires_at": expires_at,
                    "updated_at": datetime.now(UTC),
                },
            )
        )
        await self._session.execute(stmt)

    # ------------------------------------------------------------------
    # get_fresh_token
    # ------------------------------------------------------------------

    async def get_fresh_token(
        self,
        user_id: str,
        provider: str,
        *,
        refresher: TokenRefresher | None = None,
        _now: datetime | None = None,
    ) -> str:
        """Return a valid access token for (user_id, provider).

        If the stored token is within *refresh_window_seconds* of expiry
        and *refresher* is provided, the refresh flow is executed and the
        new token data is upserted before returning.

        Args:
            _now: Override current time (injected by tests for determinism).

        Raises:
            ConnectionMiss: if no connection exists for (user_id, provider).
        """
        row = await self._load_row(user_id, provider)

        token_data = json.loads(self._envelope.decrypt(row.encrypted_blob))

        if refresher is not None and _needs_refresh(
            row.expires_at, self._refresh_window, _now=_now
        ):
            token_data = await refresher(token_data)
            await self.upsert(user_id, provider, token_data)

        return token_data["access_token"]

    # ------------------------------------------------------------------
    # delete
    # ------------------------------------------------------------------

    async def delete(self, user_id: str, provider: str) -> None:
        """Remove the connection for (user_id, provider).  No-op if absent."""
        stmt = delete(OAuthConnection).where(
            OAuthConnection.user_id == user_id,
            OAuthConnection.provider == provider,
        )
        await self._session.execute(stmt)

    # ------------------------------------------------------------------
    # list
    # ------------------------------------------------------------------

    async def list(self, user_id: str) -> list[ConnectionInfo]:
        """Return metadata for all connections belonging to *user_id*.

        Token data is never included in the response.
        """
        stmt = select(OAuthConnection).where(OAuthConnection.user_id == user_id)
        result = await self._session.execute(stmt)
        rows = result.scalars().all()
        return [
            ConnectionInfo(
                user_id=row.user_id,
                provider=row.provider,
                expires_at=row.expires_at,
                created_at=row.created_at,
                updated_at=row.updated_at,
            )
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _load_row(self, user_id: str, provider: str) -> OAuthConnection:
        stmt = select(OAuthConnection).where(
            OAuthConnection.user_id == user_id,
            OAuthConnection.provider == provider,
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is None:
            raise ConnectionMiss(user_id, provider)
        return row


# ---------------------------------------------------------------------------
# Module-level façade
# ---------------------------------------------------------------------------


async def get_token(
    user_id: str,
    provider: str,
    refresher: TokenRefresher | None,
    *,
    session_factory: Any = None,
    envelope: KmsEnvelope | None = None,
) -> str:
    """High-level facade: return a fresh access token for (user_id, provider).

    Opens a session from *session_factory* (defaults to the module-level engine
    pool), constructs a ConnectionStore, and delegates to get_fresh_token.

    Pass *refresher=None* for providers without a refresh flow (e.g. Slack bot
    tokens, GitHub fine-grained PATs) — the stored token is returned as-is.

    Pass *session_factory* and *envelope* to override the module-level defaults
    — useful in tests that need to inject pre-seeded sessions without touching
    the real DB pool or KMS.

    Raises:
        ConnectionMiss: if no connection exists for (user_id, provider).
    """
    if session_factory is None:
        from app.db.engine import async_session_factory  # noqa: PLC0415

        session_factory = async_session_factory
    if envelope is None:
        from app.crypto.envelope_factory import kms_envelope_from_settings  # noqa: PLC0415

        envelope = kms_envelope_from_settings()

    async with session_factory() as session:
        store = ConnectionStore(session=session, envelope=envelope)
        return await store.get_fresh_token(user_id, provider, refresher=refresher)


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _parse_expires_at(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        dt = datetime.fromisoformat(value)
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
    return None


def _needs_refresh(
    expires_at: datetime | None,
    window: timedelta,
    *,
    _now: datetime | None = None,
) -> bool:
    if expires_at is None:
        return False
    now = _now if _now is not None else datetime.now(UTC)
    return expires_at <= now + window
