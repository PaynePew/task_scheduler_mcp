"""job_runs_error_code: add error_code column to job_runs

Propagates ActionResult.error_code into a dedicated column so task.status.v1
can surface the canonical error envelope (code + message) for structured
failures like MISSING_CONNECTION (ADR-060, issue #211).

Revision ID: 0009
Revises: 0008
Create Date: 2026-05-23

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("job_runs", sa.Column("error_code", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("job_runs", "error_code")
