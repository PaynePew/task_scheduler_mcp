"""Unit tests for the effectively-once dedup seam (ADR-070, issue #272).

Covers the pure decision function (``decide_send``), the stable key derivation
(``derive_idempotency_key``), and the in-memory store's send→sent→skip
transitions. The Postgres-backed store is exercised in the integration suite.
"""

from __future__ import annotations

import pytest

from app.actions.send_dedup import (
    ATTEMPTING,
    SENT,
    SendDecision,
    decide_send,
    derive_idempotency_key,
)
from tests.fixtures.dedup import InMemoryDedupStore

# ---------------------------------------------------------------------------
# Key derivation
# ---------------------------------------------------------------------------


def test_key_is_stable_for_same_action_and_run():
    assert derive_idempotency_key("email_send", 42) == derive_idempotency_key("email_send", 42)


def test_key_differs_by_run_id():
    assert derive_idempotency_key("email_send", 1) != derive_idempotency_key("email_send", 2)


def test_key_differs_by_action():
    assert derive_idempotency_key("email_send", 1) != derive_idempotency_key("slack_post", 1)


# ---------------------------------------------------------------------------
# Pure dedup decision
# ---------------------------------------------------------------------------


def test_no_prior_intent_sends():
    assert decide_send(None) is SendDecision.send


def test_confirmed_sent_skips():
    assert decide_send(SENT) is SendDecision.skip


def test_unconfirmed_attempt_resends():
    """A prior 'attempting' that never confirmed → resend (at-least-once bias)."""
    assert decide_send(ATTEMPTING) is SendDecision.resend


# ---------------------------------------------------------------------------
# In-memory store transitions (mirrors PostgresDedupStore semantics)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_begin_sends_second_after_sent_skips():
    store = InMemoryDedupStore()
    key = derive_idempotency_key("email_send", 7)

    first = await store.begin(key, run_id=7)
    assert first.decision is SendDecision.send

    await store.mark_sent(key, provider_message_id="msg-xyz")

    second = await store.begin(key, run_id=7)
    assert second.decision is SendDecision.skip
    assert second.provider_message_id == "msg-xyz"


@pytest.mark.asyncio
async def test_begin_without_confirmation_resends():
    store = InMemoryDedupStore()
    key = derive_idempotency_key("email_send", 8)

    await store.begin(key, run_id=8)  # attempt started, never confirmed
    again = await store.begin(key, run_id=8)

    assert again.decision is SendDecision.resend
    assert again.provider_message_id is None
