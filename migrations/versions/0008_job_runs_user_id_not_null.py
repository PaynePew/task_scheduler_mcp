"""job_runs_user_id_not_null: back-fill remaining NULLs and tighten to NOT NULL

Migration 0005 added job_runs.user_id but left it NULLABLE because no
application code populated it on new inserts.  Insert sites now set user_id
from the parent Job (Refs #162), so this migration:

  1. Back-fills any remaining NULL rows (post-0005 inserts that ran before the
     code fix).  Reuses the same correlated sub-select as 0005's upgrade.
  2. Verifies the back-fill left zero NULLs (safety gate — an orphaned run
     with no resolvable parent would block the migration rather than silently
     producing a broken NOT NULL column).
  3. ALTERs the column to NOT NULL.

Idempotent: the UPDATE is a no-op when no NULLs remain, and Postgres accepts
SET NOT NULL on a column that already has the constraint.

Revision ID: 0008
Revises: 0007
Create Date: 2026-05-21

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()

    # 1. Back-fill any post-0005 rows that still have NULL user_id.
    conn.execute(
        sa.text(
            "UPDATE job_runs"
            " SET user_id = (SELECT user_id FROM jobs WHERE jobs.job_id = job_runs.job_id)"
            " WHERE user_id IS NULL"
        )
    )

    # 2. Verify the back-fill is complete before tightening the constraint.
    null_count = conn.execute(
        sa.text("SELECT COUNT(*) FROM job_runs WHERE user_id IS NULL")
    ).scalar()
    if null_count:
        raise RuntimeError(
            f"Migration 0008: {null_count} job_runs rows still have NULL user_id "
            "after back-fill — cannot set NOT NULL.  Investigate orphaned runs."
        )

    # 3. Tighten the column to NOT NULL.
    conn.execute(sa.text("ALTER TABLE job_runs ALTER COLUMN user_id SET NOT NULL"))


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("ALTER TABLE job_runs ALTER COLUMN user_id DROP NOT NULL"))
