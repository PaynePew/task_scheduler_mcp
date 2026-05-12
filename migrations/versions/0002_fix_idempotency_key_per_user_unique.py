"""fix idempotency_key unique constraint to (user_id, idempotency_key)

The original global UNIQUE(idempotency_key) prevents different users from
sharing an idempotency key, contradicting the domain intent (per ADR-015 +
CONTEXT.md §5) that idempotency is scoped per user_id.

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-12

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("uq_jobs_idempotency_key", "jobs", type_="unique")
    op.create_unique_constraint("uq_jobs_user_idempotency", "jobs", ["user_id", "idempotency_key"])


def downgrade() -> None:
    op.drop_constraint("uq_jobs_user_idempotency", "jobs", type_="unique")
    op.create_unique_constraint("uq_jobs_idempotency_key", "jobs", ["idempotency_key"])
