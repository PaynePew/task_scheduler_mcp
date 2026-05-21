"""oauth_connections: per-user encrypted OAuth token storage (ADR-054, ADR-050).

Adds the ``oauth_connections`` table keyed by (user_id, provider).
Token data is stored as a KMS envelope-encrypted blob — no plaintext
token bytes in the DB row.

Revision ID: 0006
Revises: 0005
Create Date: 2026-05-21

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "oauth_connections",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("encrypted_blob", sa.LargeBinary(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "provider", name="uq_oauth_connections_user_provider"),
    )
    op.create_index(
        "idx_oauth_connections_user_id",
        "oauth_connections",
        ["user_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_oauth_connections_user_id", table_name="oauth_connections")
    op.drop_table("oauth_connections")
