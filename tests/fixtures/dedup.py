"""In-memory DedupStore for unit tests — no Postgres, same decision logic.

Mirrors ``app.actions.send_dedup.PostgresDedupStore`` semantics (write-ahead
intent → decide → mark sent) with a dict, so handler unit tests can exercise the
effectively-once path without a real database. The integration suite exercises
the Postgres-backed store for real.
"""

from __future__ import annotations

from app.actions.send_dedup import ATTEMPTING, SENT, SendDecision, SendGate, decide_send


class InMemoryDedupStore:
    """Dict-backed DedupStore; ``sends`` counts how many keys reached ``send``/``resend``."""

    def __init__(self) -> None:
        self._rows: dict[str, dict] = {}
        self.sends = 0

    async def begin(self, key: str, run_id: int) -> SendGate:
        existing = self._rows.get(key)
        if existing is None:
            self._rows[key] = {"status": ATTEMPTING, "provider_message_id": None}
            self.sends += 1
            return SendGate(SendDecision.send)
        decision = decide_send(existing["status"])
        if decision is SendDecision.resend:
            self.sends += 1
        provider_message_id = (
            existing["provider_message_id"] if decision is SendDecision.skip else None
        )
        return SendGate(decision, provider_message_id)

    async def mark_sent(self, key: str, provider_message_id: str | None) -> None:
        row = self._rows.setdefault(key, {"status": ATTEMPTING, "provider_message_id": None})
        row["status"] = SENT
        row["provider_message_id"] = provider_message_id
