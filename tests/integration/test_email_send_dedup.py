"""Integration tests for email_send effectively-once dedup (ADR-070, issue #272).

Drives the real Postgres-backed ``PostgresDedupStore`` (the ``send_intents``
table) — no faked store seam (anti-pattern #10). Covers the store's
begin/mark_sent transitions and the end-to-end replay guarantee: dispatching the
same ``run_id`` twice calls Gmail exactly once.

Run with:
    docker compose up -d postgres elasticmq && alembic upgrade head
    uv run pytest -m integration tests/integration/test_email_send_dedup.py
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.actions.email_send import EmailSendHandler, EmailSendParams
from app.actions.send_dedup import (
    SENT,
    PostgresDedupStore,
    SendDecision,
    derive_idempotency_key,
)
from app.db.engine import create_async_engine
from app.db.models import SendIntent


@dataclass
class FakeRun:
    user_id: str = "dedup-itest"
    run_id: int = 900_001


@pytest_asyncio.fixture
async def session_factory():
    """Fresh engine per test; cleans send_intents on teardown."""
    engine = create_async_engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    async with factory() as session:
        async with session.begin():
            await session.execute(text("DELETE FROM send_intents"))
    await engine.dispose()


def _mock_gmail_client(response: httpx.Response) -> tuple[MagicMock, AsyncMock]:
    post = AsyncMock(return_value=response)
    client = MagicMock()
    client.post = post
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=None)
    return ctx, post


async def _get_intent(factory, key: str) -> SendIntent | None:
    async with factory() as session:
        return (
            await session.execute(select(SendIntent).where(SendIntent.idempotency_key == key))
        ).scalar_one_or_none()


# ---------------------------------------------------------------------------
# Store transitions against real Postgres
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_store_begin_then_mark_sent_then_skip(session_factory):
    store = PostgresDedupStore(session_factory=session_factory)
    key = derive_idempotency_key("email_send", 900_010)

    first = await store.begin(key, run_id=900_010)
    assert first.decision is SendDecision.send

    intent = await _get_intent(session_factory, key)
    assert intent is not None
    assert intent.status == "attempting"

    await store.mark_sent(key, provider_message_id="gmail-msg-1")

    second = await store.begin(key, run_id=900_010)
    assert second.decision is SendDecision.skip
    assert second.provider_message_id == "gmail-msg-1"

    intent = await _get_intent(session_factory, key)
    assert intent.status == SENT
    assert intent.provider_message_id == "gmail-msg-1"


@pytest.mark.integration
async def test_store_second_begin_before_confirm_resends(session_factory):
    store = PostgresDedupStore(session_factory=session_factory)
    key = derive_idempotency_key("email_send", 900_011)

    await store.begin(key, run_id=900_011)  # attempt started, never confirmed
    again = await store.begin(key, run_id=900_011)

    assert again.decision is SendDecision.resend
    assert again.provider_message_id is None


# ---------------------------------------------------------------------------
# End-to-end replay: same run_id → Gmail called once
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_replay_same_run_id_sends_once(session_factory):
    handler = EmailSendHandler(dedup_store=PostgresDedupStore(session_factory=session_factory))
    params = EmailSendParams(to=["dest@example.com"], subject="Digest", body="Body text")
    run = FakeRun(run_id=900_020)

    ctx, post = _mock_gmail_client(httpx.Response(200, json={"id": "gmail-abc"}))

    with (
        patch("app.actions.email_send.settings") as mock_settings,
        patch("app.actions.email_send.get_token", AsyncMock(return_value="ya29.tok")),
        patch("app.actions.email_send.httpx.AsyncClient", return_value=ctx),
    ):
        mock_settings.operator_user_id = "operator-uid"
        mock_settings.connections_base_url = "http://localhost:8000"

        first = await handler.execute(run=run, params=params)
        second = await handler.execute(run=run, params=params)

    assert post.await_count == 1, "same run_id must reach Gmail exactly once"
    assert first.ok is True
    assert first.result["provider_message_id"] == "gmail-abc"
    assert second.ok is True
    assert second.result.get("deduped") is True
    assert second.result["provider_message_id"] == "gmail-abc"

    key = derive_idempotency_key("email_send", run.run_id)
    intent = await _get_intent(session_factory, key)
    assert intent is not None
    assert intent.status == SENT
    assert intent.provider_message_id == "gmail-abc"
