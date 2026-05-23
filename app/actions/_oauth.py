"""OAuth connection helper for execute() paths (issue #211, Layer 3).

Called at the top of each OAuth-gated execute() to check connection validity
and attempt refresh before returning MISSING_CONNECTION.  This surfaces the
same canonical error code that task.create preflight uses (ADR-058, ADR-060).

When KMS is not configured (dev/CI), the check is skipped and the caller falls
through to the standard get_token path (which raises ConnectionMiss on a
missing row, caught and wrapped below).

Production flow (KMS configured):
  1. Load the OAuthConnection row (no decryption needed — expires_at is plaintext).
  2. If the row is missing → MISSING_CONNECTION.
  3. If expires_at is in the past and no refresher → MISSING_CONNECTION.
  4. If expires_at is in the past and refresher provided → attempt refresh;
     on failure → MISSING_CONNECTION.
  5. Otherwise → connection is valid, return None (caller proceeds normally).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.actions.base import ActionResult
from app.config.settings import settings
from app.connections.store import ConnectionMiss, ConnectionStore, TokenRefresher
from app.crypto.envelope_factory import kms_envelope_from_settings
from app.db.engine import async_session_factory
from app.db.models import OAuthConnection


def _make_missing_connection(provider: str) -> ActionResult:
    connect_url = f"{settings.connections_base_url}/connections"
    return ActionResult(
        ok=False,
        result=None,
        error=(
            f"Your {provider.capitalize()} connection has expired or been revoked. "
            f"Reconnect at {connect_url}"
        ),
        error_code="MISSING_CONNECTION",
        retryable=False,
    )


async def _load_row(
    user_id: str,
    provider: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> OAuthConnection | None:
    async with session_factory() as session:
        result = await session.execute(
            select(OAuthConnection).where(
                OAuthConnection.user_id == user_id,
                OAuthConnection.provider == provider,
            )
        )
        return result.scalar_one_or_none()


async def check_oauth_for_execute(
    user_id: str,
    provider: str,
    *,
    refresher: TokenRefresher | None = None,
    _now: datetime | None = None,
) -> ActionResult | None:
    """Check OAuth connection validity at execute() time.

    Returns None when the connection is present and not expired (caller proceeds).
    Returns an ActionResult(error_code="MISSING_CONNECTION") when the connection
    is absent, expired without a refresher, or when refresh fails.

    When KMS is not configured the check is skipped (returns None) so the
    existing get_token / ConnectionMiss path handles dev/CI mode unchanged.
    """
    envelope = kms_envelope_from_settings()
    if envelope is None:
        # Dev/CI mode: skip — existing handler get_token path handles this.
        return None

    row = await _load_row(user_id, provider, async_session_factory)
    if row is None:
        return _make_missing_connection(provider)

    now = _now or datetime.now(UTC)
    if row.expires_at is not None and row.expires_at <= now:
        if refresher is not None:
            # Best-effort refresh: on any failure, fall through to MISSING_CONNECTION.
            try:
                async with async_session_factory() as session:
                    store = ConnectionStore(session=session, envelope=envelope)
                    await store.get_fresh_token(user_id, provider, refresher=refresher)
                return None  # Refresh succeeded; caller proceeds normally.
            except Exception:
                pass
        return _make_missing_connection(provider)

    return None  # Connection is present and not expired.


# Expose _load_row and async_session_factory at module level so tests can patch them.
__all__ = ["check_oauth_for_execute"]
