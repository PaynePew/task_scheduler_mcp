"""w2_schema: add cancelled_at to jobs; drop raw_user_input and parsing_metadata

These NL-parser columns were scaffolded in W1 as "schema-frozen bonus columns"
but ADR-023 removed the server-side NL parser from scope entirely. Dropping them
now (W2) keeps the schema honest and avoids phantom nullable columns that no code
ever writes to. The cancelled_at column is the idempotency anchor for the new
best-effort cancel semantics (ADR-022): once set, re-cancel is a no-op instead of
raising INVALID_STATE.

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-16

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True))
    op.drop_column("jobs", "raw_user_input")
    op.drop_column("jobs", "parsing_metadata")


def downgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column(
            "parsing_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.add_column("jobs", sa.Column("raw_user_input", sa.Text(), nullable=True))
    op.drop_column("jobs", "cancelled_at")
