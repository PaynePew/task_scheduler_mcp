"""migrate_default_user_to_operator: reassign default-user rows to OPERATOR_USER_ID

One-shot data migration per ADR-059: rewrites every jobs.user_id and job_runs.user_id
equal to 'default-user' to the value of the OPERATOR_USER_ID environment variable.
This associates the operator's pre-migration jobs with their verified WorkOS identity.

The migration also adds a denormalized user_id column to job_runs so runs can be
queried by owner without always joining jobs.

Idempotent: if no 'default-user' rows remain, re-running is a no-op. The column
addition uses IF NOT EXISTS so it is safe to run more than once.

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-20

"""

from __future__ import annotations

import os
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DEFAULT_USER = "default-user"


def _operator_user_id() -> str:
    return os.environ.get("OPERATOR_USER_ID", _DEFAULT_USER)


def upgrade() -> None:
    operator_uid = _operator_user_id()
    conn = op.get_bind()

    # 1. Add user_id to job_runs (idempotent via IF NOT EXISTS).
    conn.execute(sa.text("ALTER TABLE job_runs ADD COLUMN IF NOT EXISTS user_id TEXT"))

    # 2. Back-fill job_runs.user_id from the parent job for any NULL rows
    #    (handles both fresh adds and re-runs where some rows already have values).
    conn.execute(
        sa.text(
            "UPDATE job_runs"
            " SET user_id = (SELECT user_id FROM jobs WHERE jobs.job_id = job_runs.job_id)"
            " WHERE job_runs.user_id IS NULL"
        )
    )

    # 3. Migrate jobs: default-user → OPERATOR_USER_ID.
    conn.execute(
        sa.text("UPDATE jobs SET user_id = :uid WHERE user_id = :old").bindparams(
            uid=operator_uid, old=_DEFAULT_USER
        )
    )

    # 4. Migrate job_runs: default-user → OPERATOR_USER_ID.
    conn.execute(
        sa.text("UPDATE job_runs SET user_id = :uid WHERE user_id = :old").bindparams(
            uid=operator_uid, old=_DEFAULT_USER
        )
    )


def downgrade() -> None:
    operator_uid = _operator_user_id()
    conn = op.get_bind()

    # Reverse the data migration in both tables.
    conn.execute(
        sa.text("UPDATE jobs SET user_id = :old WHERE user_id = :uid").bindparams(
            uid=operator_uid, old=_DEFAULT_USER
        )
    )
    conn.execute(
        sa.text("UPDATE job_runs SET user_id = :old WHERE user_id = :uid").bindparams(
            uid=operator_uid, old=_DEFAULT_USER
        )
    )

    # Remove the denormalized column added in upgrade.
    conn.execute(sa.text("ALTER TABLE job_runs DROP COLUMN IF EXISTS user_id"))
