"""Unit tests for email_send effectively-once behavior (ADR-070, issue #272).

Drives the handler's dedup gate with an in-memory store: a second dispatch with
the same ``run_id`` must NOT call Gmail again (no duplicate send) and must still
report success, echoing the first send's provider message id.
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.actions.email_send import EmailSendHandler, EmailSendParams
from tests.fixtures.dedup import InMemoryDedupStore


@dataclass
class FakeRun:
    user_id: str = "user-abc"
    run_id: int = 100


def _build_mock_http_client(response: httpx.Response) -> tuple[MagicMock, AsyncMock]:
    """Return (client_context, post_mock) so tests can assert the call count."""
    post = AsyncMock(return_value=response)
    mock_client = MagicMock()
    mock_client.post = post
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_client)
    ctx.__aexit__ = AsyncMock(return_value=None)
    return ctx, post


@pytest.mark.asyncio
async def test_replay_same_run_id_sends_once():
    """Two dispatches of the same run → exactly one Gmail POST; second is a dedup no-op."""
    handler = EmailSendHandler(dedup_store=InMemoryDedupStore())
    params = EmailSendParams(to=["dest@example.com"], subject="Test", body="Body text")
    run = FakeRun(run_id=100)

    success_resp = httpx.Response(200, json={"id": "msg-abc"})
    ctx, post = _build_mock_http_client(success_resp)

    with (
        patch("app.actions.email_send.settings") as mock_settings,
        patch("app.actions.email_send.get_token", AsyncMock(return_value="ya29.tok")),
        patch("app.actions.email_send.httpx.AsyncClient", return_value=ctx),
    ):
        mock_settings.operator_user_id = "operator-uid"
        mock_settings.connections_base_url = "http://localhost:8000"

        first = await handler.execute(run=run, params=params)
        second = await handler.execute(run=run, params=params)

    assert post.await_count == 1, "Gmail must be called exactly once across the replay"

    assert first.ok is True
    assert first.result["provider_message_id"] == "msg-abc"
    assert first.result.get("deduped") is not True

    assert second.ok is True
    assert second.result.get("deduped") is True
    assert second.result["provider_message_id"] == "msg-abc"


@pytest.mark.asyncio
async def test_distinct_run_ids_each_send():
    """Different run_ids are independent logical sends → each calls Gmail."""
    handler = EmailSendHandler(dedup_store=InMemoryDedupStore())
    params = EmailSendParams(to=["dest@example.com"], subject="Test", body="Body")

    success_resp = httpx.Response(200, json={"id": "msg-1"})
    ctx, post = _build_mock_http_client(success_resp)

    with (
        patch("app.actions.email_send.settings") as mock_settings,
        patch("app.actions.email_send.get_token", AsyncMock(return_value="ya29.tok")),
        patch("app.actions.email_send.httpx.AsyncClient", return_value=ctx),
    ):
        mock_settings.operator_user_id = "operator-uid"
        mock_settings.connections_base_url = "http://localhost:8000"

        await handler.execute(run=FakeRun(run_id=1), params=params)
        await handler.execute(run=FakeRun(run_id=2), params=params)

    assert post.await_count == 2


@pytest.mark.asyncio
async def test_failed_send_leaves_intent_open_so_retry_resends():
    """A transient failure must not mark 'sent' — the redelivery re-sends (at-least-once)."""
    store = InMemoryDedupStore()
    handler = EmailSendHandler(dedup_store=store)
    params = EmailSendParams(to=["dest@example.com"], subject="Test", body="Body")
    run = FakeRun(run_id=55)

    fail_resp = httpx.Response(500, json={"error": "backendError"})
    ok_resp = httpx.Response(200, json={"id": "msg-after-retry"})
    post = AsyncMock(side_effect=[fail_resp, ok_resp])
    mock_client = MagicMock()
    mock_client.post = post
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_client)
    ctx.__aexit__ = AsyncMock(return_value=None)

    with (
        patch("app.actions.email_send.settings") as mock_settings,
        patch("app.actions.email_send.get_token", AsyncMock(return_value="ya29.tok")),
        patch("app.actions.email_send.httpx.AsyncClient", return_value=ctx),
    ):
        mock_settings.operator_user_id = "operator-uid"
        mock_settings.connections_base_url = "http://localhost:8000"

        first = await handler.execute(run=run, params=params)
        second = await handler.execute(run=run, params=params)

    assert first.ok is False and first.retryable is True
    assert second.ok is True
    assert second.result["provider_message_id"] == "msg-after-retry"
    assert post.await_count == 2, "the retry after a transient failure must re-send"
