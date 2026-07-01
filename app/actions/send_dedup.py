"""Effectively-once dedup for non-idempotent external sends (ADR-070, PRD #266).

SQS gives at-least-once *delivery*; exactly-once end-to-end for an external
side effect (a real email) is impossible (two-generals). We shrink the
double-send window to a single unavoidable instant with three pieces:

1. a stable, **run-derived idempotency key** (``derive_idempotency_key``),
2. a **write-ahead intent** — a durable row keyed on that key, written
   ``attempting`` before the provider call, and
3. an **app-side dedup decision** (``decide_send``) — on replay, a row already
   marked ``sent`` means no-op; a row still ``attempting`` means a prior attempt
   started but never confirmed.

``decide_send`` is the pure seam (unit-tested); ``PostgresDedupStore`` is the
durable store (integration-tested against real Postgres). The handler
(``app.actions.email_send``) owns *what* the provider call is; this module owns
*whether* it should happen.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import SendIntent

# Intent-record statuses (see SendIntent.status).
ATTEMPTING = "attempting"
SENT = "sent"


def derive_idempotency_key(action: str, run_id: int) -> str:
    """Stable dedup key for one logical send.

    Derived only from the action name + ``run_id`` so every replay of the same
    run computes the same key — the property the whole scheme rests on.
    """
    return f"{action}:{run_id}"


class SendDecision(StrEnum):
    """What the dedup gate says to do with a pending send.

    - ``send``   — no prior intent: write-ahead the intent, then call the provider.
    - ``resend`` — a prior attempt wrote ``attempting`` but never confirmed
                   ``sent``. At-least-once bias (ADR-070): call the provider
                   again, tolerating an extremely rare duplicate.
    - ``skip``   — a prior attempt confirmed ``sent``: no-op, do not call the
                   provider (this is the dedup hit that prevents the double-send).
    """

    send = "send"
    resend = "resend"
    skip = "skip"


def decide_send(existing_status: str | None) -> SendDecision:
    """Pure dedup decision from the stored intent status (unit-tested seam)."""
    if existing_status is None:
        return SendDecision.send
    if existing_status == SENT:
        return SendDecision.skip
    return SendDecision.resend


@dataclass(frozen=True)
class SendGate:
    """Outcome of opening the dedup gate for a key.

    ``provider_message_id`` is populated only on a ``skip`` — it is the id the
    prior confirmed send stored, so the handler can echo it without re-sending.
    """

    decision: SendDecision
    provider_message_id: str | None = None


class DedupStore(Protocol):
    """The seam the send handler depends on — real store or an in-memory fake."""

    async def begin(self, key: str, run_id: int) -> SendGate: ...

    async def mark_sent(self, key: str, provider_message_id: str | None) -> None: ...


class PostgresDedupStore:
    """Durable dedup store backed by the ``send_intents`` table.

    ``begin`` write-aheads the intent with an atomic INSERT ... ON CONFLICT DO
    NOTHING: the row's primary key is the idempotency key, so exactly one caller
    wins the insert (``send``); a conflict means a prior attempt already wrote
    the intent, and its committed status decides ``skip`` vs ``resend``.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession] | None = None) -> None:
        # Deferred import keeps this module usable (and unit tests fast) without
        # constructing the module-level runtime engine at import time.
        if session_factory is None:
            from app.db.engine import async_session_factory  # noqa: PLC0415

            session_factory = async_session_factory
        self._session_factory = session_factory

    async def begin(self, key: str, run_id: int) -> SendGate:
        async with self._session_factory() as session:
            async with session.begin():
                inserted = (
                    await session.execute(
                        pg_insert(SendIntent)
                        .values(idempotency_key=key, run_id=run_id, status=ATTEMPTING)
                        .on_conflict_do_nothing(index_elements=["idempotency_key"])
                        .returning(SendIntent.idempotency_key)
                    )
                ).first()
                if inserted is not None:
                    return SendGate(SendDecision.send)

                existing = (
                    await session.execute(
                        select(SendIntent.status, SendIntent.provider_message_id).where(
                            SendIntent.idempotency_key == key
                        )
                    )
                ).first()

        status = existing.status if existing is not None else None
        decision = decide_send(status)
        provider_message_id = (
            existing.provider_message_id
            if existing is not None and decision is SendDecision.skip
            else None
        )
        return SendGate(decision, provider_message_id)

    async def mark_sent(self, key: str, provider_message_id: str | None) -> None:
        now = datetime.now(tz=UTC)
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(
                    update(SendIntent)
                    .where(SendIntent.idempotency_key == key)
                    .values(status=SENT, provider_message_id=provider_message_id, updated_at=now)
                )
