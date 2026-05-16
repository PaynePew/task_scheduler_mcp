"""Integration test for the w2_schema alembic migration (revision 0003).

Exercises the full upgrade head → downgrade -1 → upgrade head roundtrip and
asserts schema state at each step. Requires a running Postgres instance with
ALEMBIC_DATABASE_URL set (provided by the harness).

Run with:
    uv run pytest -m integration tests/integration/test_alembic_migration.py
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest
import sqlalchemy as sa
from sqlalchemy import text


def _run_alembic(*args: str) -> subprocess.CompletedProcess:
    """Run an alembic command and return the CompletedProcess (raises on failure)."""
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        capture_output=True,
        text=True,
        cwd=os.environ.get("ALEMBIC_CWD", "/workspace"),
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"alembic {' '.join(args)} failed (exit {result.returncode}):\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
    return result


def _get_jobs_columns(url: str) -> set[str]:
    """Return the set of column names in the jobs table via a synchronous connection."""
    engine = sa.create_engine(url, poolclass=sa.pool.NullPool)
    try:
        with engine.connect() as conn:
            result = conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_schema = 'public' AND table_name = 'jobs'"
                )
            )
            return {row[0] for row in result}
    finally:
        engine.dispose()


@pytest.mark.integration
def test_w2_schema_migration_roundtrip():
    """upgrade head → downgrade -1 → upgrade head asserts schema at each step."""
    url = os.environ["ALEMBIC_DATABASE_URL"]

    # Step 1: upgrade head — 0003 must be applied.
    _run_alembic("upgrade", "head")
    cols_after_upgrade = _get_jobs_columns(url)

    assert "cancelled_at" in cols_after_upgrade
    assert "raw_user_input" not in cols_after_upgrade
    assert "parsing_metadata" not in cols_after_upgrade

    # Step 2: downgrade -1 — reverts 0003, restoring raw_user_input/parsing_metadata.
    _run_alembic("downgrade", "-1")
    cols_after_downgrade = _get_jobs_columns(url)

    assert "cancelled_at" not in cols_after_downgrade
    assert "raw_user_input" in cols_after_downgrade
    assert "parsing_metadata" in cols_after_downgrade

    # Step 3: upgrade head again — idempotent re-apply of 0003.
    _run_alembic("upgrade", "head")
    cols_after_re_upgrade = _get_jobs_columns(url)

    assert "cancelled_at" in cols_after_re_upgrade
    assert "raw_user_input" not in cols_after_re_upgrade
    assert "parsing_metadata" not in cols_after_re_upgrade
