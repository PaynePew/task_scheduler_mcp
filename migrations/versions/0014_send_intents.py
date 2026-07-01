"""send_intents: durable dedup/intent table for effectively-once sends

Issue #272 (PRD #266, execution-plane durability; ADR-070). ``email_send`` is a
non-idempotent external side effect: a redelivered or reconciler-retried run
could double-send. This table gives each logical send a durable record keyed on
a run-derived idempotency key (``f"{action}:{run_id}"``): the handler writes
``attempting`` before the provider call and flips it to ``sent`` + the provider
message id after. A replay reads the row and no-ops if already ``sent``.

The primary key on ``idempotency_key`` is the uniqueness that makes the
write-ahead insert (INSERT ... ON CONFLICT DO NOTHING) atomic.

Revision ID: 0014
Revises: 0013
Create Date: 2026-07-01

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "send_intents",
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="attempting"),
        sa.Column("provider_message_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("idempotency_key", name="pk_send_intents"),
    )


def downgrade() -> None:
    op.drop_table("send_intents")
