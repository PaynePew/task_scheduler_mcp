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

All module-level imports of the async engine / KMS envelope are intentionally
deferred to inside the function so each invocation picks up the correct event
loop (pytest-asyncio uses a per-test loop — a module-level engine would be
attached to the first test's loop and cause "Future attached to a different
loop" errors on subsequent tests).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from app.actions.base import ActionResult
from app.config.settings import settings
from app.connections.store import ConnectionStore, TokenRefresher
from app.crypto.envelope_factory import kms_envelope_from_settings
from app.db.models import OAuthConnection

logger = logging.getLogger(__name__)


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


async def check_oauth_for_execute(
    user_id: str,
    provider: str,
    *,
    refresher: TokenRefresher | None = None,
    _session_factory: Any = None,
    _now: datetime | None = None,
) -> ActionResult | None:
    """Check OAuth connection validity at execute() time.

    Returns None when the connection is present and not expired (caller proceeds).
    Returns an ActionResult(error_code="MISSING_CONNECTION") when the connection
    is absent, expired without a refresher, or when refresh fails.

    When KMS is not configured the check is skipped (returns None) so the
    existing get_token / ConnectionMiss path handles dev/CI mode unchanged.

    ``_session_factory`` is injectable for tests that need a per-test engine to
    avoid "Future attached to a different loop" errors when tests share the
    module-level async_session_factory across different asyncio event loops.
    """
    if _session_factory is None:
        # Lazy import so each test invocation picks up the current module state
        # (supports patching app.db.engine.async_session_factory in tests).
        from app.db.engine import async_session_factory as _sf  # noqa: PLC0415

        _session_factory = _sf

    envelope = kms_envelope_from_settings()
    if envelope is None:
        # Dev/CI mode: skip — existing handler get_token path handles this.
        return None

    # Load the row using plaintext expires_at (no decryption needed for the check).
    async with _session_factory() as session:
        result = await session.execute(
            select(OAuthConnection).where(
                OAuthConnection.user_id == user_id,
                OAuthConnection.provider == provider,
            )
        )
        row = result.scalar_one_or_none()

    if row is None:
        return _make_missing_connection(provider)

    now = _now or datetime.now(UTC)
    if row.expires_at is not None and row.expires_at <= now:
        if refresher is not None:
            # Best-effort refresh: log + fall through to MISSING_CONNECTION on any failure
            # (revoked refresh token, provider outage). The user must reconnect either way.
            try:
                async with _session_factory() as session:
                    store = ConnectionStore(session=session, envelope=envelope)
                    await store.get_fresh_token(user_id, provider, refresher=refresher)
                return None  # Refresh succeeded; caller proceeds normally.
            except Exception:
                logger.exception(
                    "oauth refresh failed for user=%s provider=%s; returning MISSING_CONNECTION",
                    user_id,
                    provider,
                )
        return _make_missing_connection(provider)

    return None  # Connection is present and not expired.


__all__ = ["check_oauth_for_execute"]
