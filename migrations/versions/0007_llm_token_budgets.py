"""llm_token_budgets: per-user daily token-spend tracker for LLM actions

New table supporting ADR-052 operator-subsidized LLM actions. One row per
(user_id, budget_date) calendar day (UTC). The daily budget resets naturally —
yesterday's row is not found when querying for today's date.

Revision ID: 0007
Revises: 0006
Create Date: 2026-05-21

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "llm_token_budgets",
        sa.Column("user_id", sa.Text, primary_key=True, nullable=False),
        sa.Column("budget_date", sa.Date, primary_key=True, nullable=False),
        sa.Column("tokens_used", sa.Integer, nullable=False, server_default="0"),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("llm_token_budgets")
