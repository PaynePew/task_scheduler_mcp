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
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import text

# Repo root resolved from this test file's location so alembic finds
# alembic.ini regardless of where the test runs (harness container at
# /workspace, GHA runner at /home/runner/work/..., a developer's
# local checkout, etc.). The `/workspace` default that was previously
# hardcoded only worked inside the harness container.
_REPO_ROOT = str(Path(__file__).resolve().parents[2])


def _run_alembic(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Run an alembic command and return the CompletedProcess (raises on failure).

    Pass ``env`` to override the subprocess environment (e.g. to set
    OPERATOR_USER_ID for 0005). ``None`` inherits the parent process env.
    """
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        capture_output=True,
        text=True,
        cwd=os.environ.get("ALEMBIC_CWD", _REPO_ROOT),
        env=env,
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


def _index_exists(url: str, index_name: str) -> bool:
    """Return True if the named index exists in the public schema."""
    engine = sa.create_engine(url, poolclass=sa.pool.NullPool)
    try:
        with engine.connect() as conn:
            result = conn.execute(
                text("SELECT 1 FROM pg_indexes WHERE schemaname = 'public' AND indexname = :name"),
                {"name": index_name},
            )
            return result.fetchone() is not None
    finally:
        engine.dispose()


@pytest.mark.integration
def test_w2_schema_migration_roundtrip():
    """upgrade head → downgrade 0002 → upgrade head asserts schema at each step.

    Uses an explicit target revision (0002) so the test remains focused on the
    0003 column changes regardless of how many later migrations exist.
    """
    url = os.environ["ALEMBIC_DATABASE_URL"]

    # Step 1: upgrade head — all migrations (including 0003) must be applied.
    _run_alembic("upgrade", "head")
    cols_after_upgrade = _get_jobs_columns(url)

    assert "cancelled_at" in cols_after_upgrade
    assert "raw_user_input" not in cols_after_upgrade
    assert "parsing_metadata" not in cols_after_upgrade

    # Step 2: downgrade to 0002 — reverts 0003 (and any later migrations),
    # restoring raw_user_input/parsing_metadata.
    _run_alembic("downgrade", "0002")
    cols_after_downgrade = _get_jobs_columns(url)

    assert "cancelled_at" not in cols_after_downgrade
    assert "raw_user_input" in cols_after_downgrade
    assert "parsing_metadata" in cols_after_downgrade

    # Step 3: upgrade head again — idempotent re-apply.
    _run_alembic("upgrade", "head")
    cols_after_re_upgrade = _get_jobs_columns(url)

    assert "cancelled_at" in cols_after_re_upgrade
    assert "raw_user_input" not in cols_after_re_upgrade
    assert "parsing_metadata" not in cols_after_re_upgrade


@pytest.mark.integration
def test_0004_unique_index_roundtrip():
    """Migration 0004 creates the partial unique index; downgrade removes it."""
    url = os.environ["ALEMBIC_DATABASE_URL"]
    index_name = "uq_job_runs_job_scheduled_nonterminal"

    # Upgrade to exactly 0004 so the test targets the right revision boundary
    # regardless of how many later migrations exist.
    _run_alembic("upgrade", "0004")
    assert _index_exists(url, index_name), "Index must exist at revision 0004"

    # Downgrade to 0003 explicitly — removes the index added in 0004.
    _run_alembic("downgrade", "0003")
    assert not _index_exists(url, index_name), "Index must be absent after downgrade to 0003"

    # Re-apply head (includes 0004 and any later migrations).
    _run_alembic("upgrade", "head")
    assert _index_exists(url, index_name), "Index must exist again after re-upgrade to head"


# ---------------------------------------------------------------------------
# Migration 0005: migrate default-user rows to OPERATOR_USER_ID
# ---------------------------------------------------------------------------

_OPERATOR_UID = "workos-test-sub-op-12345"


def _run_alembic_with_operator(*args: str) -> subprocess.CompletedProcess:
    """Run alembic with OPERATOR_USER_ID set to the test value."""
    return _run_alembic(*args, env={**os.environ, "OPERATOR_USER_ID": _OPERATOR_UID})


def _count_rows(url: str, table: str, where_clause: str, params: dict) -> int:
    """Return count of rows matching a WHERE clause."""
    engine = sa.create_engine(url, poolclass=sa.pool.NullPool)
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(f"SELECT COUNT(*) FROM {table} WHERE {where_clause}"),  # noqa: S608
                params,
            ).fetchone()
            return row[0] if row else 0
    finally:
        engine.dispose()


def _get_job_run_columns(url: str) -> set[str]:
    """Return the set of column names in job_runs."""
    engine = sa.create_engine(url, poolclass=sa.pool.NullPool)
    try:
        with engine.connect() as conn:
            result = conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_schema = 'public' AND table_name = 'job_runs'"
                )
            )
            return {row[0] for row in result}
    finally:
        engine.dispose()


def _insert_default_user_job(url: str) -> int:
    """Insert a job owned by default-user and return its job_id."""
    engine = sa.create_engine(url, poolclass=sa.pool.NullPool)
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "INSERT INTO jobs"
                    " (user_id, description, action, action_params,"
                    "  job_type, scheduled_at, active, created_at, updated_at)"
                    " VALUES ('default-user', 'test', 'echo', '{}'::jsonb,"
                    "  'one_shot', NOW(), TRUE, NOW(), NOW())"
                    " RETURNING job_id"
                )
            ).fetchone()
            conn.commit()
            return row[0]
    finally:
        engine.dispose()


def _insert_job_run(url: str, job_id: int) -> None:
    """Insert a job_run for the given job_id."""
    engine = sa.create_engine(url, poolclass=sa.pool.NullPool)
    try:
        with engine.connect() as conn:
            conn.execute(
                text(
                    "INSERT INTO job_runs"
                    " (time_bucket, job_id, scheduled_at, status, retry_count, max_retries,"
                    "  created_at, updated_at)"
                    " VALUES (TO_CHAR(NOW(), 'YYYY-MM-DD HH24:00:00'), :job_id,"
                    "  NOW(), 'PENDING', 0, 3, NOW(), NOW())"
                ),
                {"job_id": job_id},
            )
            conn.commit()
    finally:
        engine.dispose()


@pytest.mark.integration
def test_0005_migrates_default_user_in_jobs():
    """After upgrade to 0005, no default-user rows remain in jobs."""
    url = os.environ["ALEMBIC_DATABASE_URL"]

    # Start at 0004 so we can insert data in the pre-migration state.
    _run_alembic_with_operator("downgrade", "0004")

    # Insert a default-user job.
    job_id = _insert_default_user_job(url)

    # Apply 0005 with OPERATOR_USER_ID set.
    _run_alembic_with_operator("upgrade", "head")

    # The job should now be owned by OPERATOR_USER_ID.
    remaining_default = _count_rows(url, "jobs", "user_id = 'default-user'", {})
    operator_owned = _count_rows(
        url, "jobs", "job_id = :jid AND user_id = :uid", {"jid": job_id, "uid": _OPERATOR_UID}
    )
    assert remaining_default == 0, f"default-user rows still in jobs: {remaining_default}"
    assert operator_owned == 1, f"job {job_id} not reassigned to operator"


@pytest.mark.integration
def test_0005_migrates_default_user_in_job_runs():
    """After upgrade to 0005, no default-user rows remain in job_runs."""
    url = os.environ["ALEMBIC_DATABASE_URL"]

    # Ensure we start at 0004.
    _run_alembic_with_operator("downgrade", "0004")

    # Insert a default-user job + one job_run.
    job_id = _insert_default_user_job(url)
    _insert_job_run(url, job_id)

    # Apply migration 0005.
    _run_alembic_with_operator("upgrade", "head")

    # job_runs.user_id column must now exist.
    cols = _get_job_run_columns(url)
    assert "user_id" in cols, "job_runs.user_id column must exist after 0005"

    # No default-user rows remain in job_runs.
    remaining = _count_rows(url, "job_runs", "user_id = 'default-user'", {})
    assert remaining == 0, f"default-user rows still in job_runs: {remaining}"

    # The run is now owned by OPERATOR_USER_ID.
    operator_runs = _count_rows(
        url, "job_runs", "job_id = :jid AND user_id = :uid", {"jid": job_id, "uid": _OPERATOR_UID}
    )
    assert operator_runs == 1, f"job_run for job {job_id} not reassigned to operator"


@pytest.mark.integration
def test_0005_migration_is_idempotent():
    """Re-running upgrade to 0005 is a no-op (no rows change on second run)."""
    url = os.environ["ALEMBIC_DATABASE_URL"]

    # Start from 0004, insert data, apply migration.
    _run_alembic_with_operator("downgrade", "0004")
    _insert_default_user_job(url)
    _run_alembic_with_operator("upgrade", "head")

    count_before = _count_rows(url, "jobs", "user_id = :uid", {"uid": _OPERATOR_UID})

    # Downgrade then re-upgrade should be a no-op on already-migrated data.
    _run_alembic_with_operator("downgrade", "0004")
    _run_alembic_with_operator("upgrade", "head")

    # After the round-trip the OPERATOR_USER_ID row count should be the same.
    count_after = _count_rows(url, "jobs", "user_id = :uid", {"uid": _OPERATOR_UID})
    # The downgrade reverts to default-user; re-upgrade migrates again. Counts match.
    assert count_after == count_before


@pytest.mark.integration
def test_0005_task_list_returns_migrated_jobs():
    """Post-migration, handle_task_list with OPERATOR_USER_ID returns pre-migration jobs."""
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.db.engine import create_async_engine
    from app.mcp.handlers.list import handle_task_list

    url = os.environ["ALEMBIC_DATABASE_URL"]

    # Start at 0004 and insert a default-user job.
    _run_alembic_with_operator("downgrade", "0004")
    job_id = _insert_default_user_job(url)

    # Apply migration 0005 — job is now owned by OPERATOR_USER_ID.
    _run_alembic_with_operator("upgrade", "head")

    async def _run():
        engine = create_async_engine()
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            result = await handle_task_list(
                {"pageSize": 100},
                user_id=_OPERATOR_UID,
                session_factory=factory,
            )
            return result
        finally:
            await engine.dispose()

    result = asyncio.run(_run())
    assert result["ok"] is True, f"task.list failed: {result}"
    job_ids = [j["job_id"] for j in result["data"]["jobs"]]
    assert job_id in job_ids, f"job {job_id} not found in task.list result for operator"


@pytest.mark.integration
def test_0005_downgrade_removes_user_id_column():
    """Downgrading from 0005 to 0004 removes the user_id column from job_runs."""
    url = os.environ["ALEMBIC_DATABASE_URL"]

    # Ensure 0005 is applied (use explicit revision, not head, so this test
    # keeps working when later migrations are added).
    _run_alembic_with_operator("upgrade", "0005")
    cols_before = _get_job_run_columns(url)
    assert "user_id" in cols_before, "user_id must exist before downgrade"

    # Downgrade to 0004 explicitly — tests migration 0005's downgrade (removes user_id).
    # Stays explicit so the test still targets the right revision boundary as more
    # migrations are added on top.
    _run_alembic_with_operator("downgrade", "0004")
    cols_after = _get_job_run_columns(url)
    assert "user_id" not in cols_after, "user_id must be removed after downgrade from 0005"

    # Re-apply so the DB is back at head for subsequent tests.
    _run_alembic_with_operator("upgrade", "head")


# ---------------------------------------------------------------------------
# Migration 0008: back-fill NULL job_runs.user_id from parent job, then NOT NULL
# ---------------------------------------------------------------------------


def _insert_job(url: str, user_id: str) -> int:
    """Insert a job with the given owner and return its job_id."""
    engine = sa.create_engine(url, poolclass=sa.pool.NullPool)
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "INSERT INTO jobs"
                    " (user_id, description, action, action_params,"
                    "  job_type, scheduled_at, active, created_at, updated_at)"
                    " VALUES (:uid, 'test', 'echo', '{}'::jsonb,"
                    "  'one_shot', NOW(), TRUE, NOW(), NOW())"
                    " RETURNING job_id"
                ),
                {"uid": user_id},
            ).fetchone()
            conn.commit()
            return row[0]
    finally:
        engine.dispose()


def _insert_job_run_null_user(url: str, job_id: int) -> int:
    """Insert a job_run with NULL user_id (only legal at revisions 0005–0007)."""
    engine = sa.create_engine(url, poolclass=sa.pool.NullPool)
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "INSERT INTO job_runs"
                    " (time_bucket, job_id, scheduled_at, status,"
                    "  retry_count, max_retries, created_at, updated_at, user_id)"
                    " VALUES (TO_CHAR(NOW(), 'YYYY-MM-DD HH24:00:00'), :job_id,"
                    "  NOW(), 'PENDING', 0, 3, NOW(), NOW(), NULL)"
                    " RETURNING run_id"
                ),
                {"job_id": job_id},
            ).fetchone()
            conn.commit()
            return row[0]
    finally:
        engine.dispose()


def _get_run_user(url: str, run_id: int) -> str | None:
    engine = sa.create_engine(url, poolclass=sa.pool.NullPool)
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT user_id FROM job_runs WHERE run_id = :rid"),
                {"rid": run_id},
            ).fetchone()
            return row[0] if row else None
    finally:
        engine.dispose()


def _user_id_is_not_null(url: str) -> bool:
    """Return True iff job_runs.user_id has a NOT NULL constraint."""
    engine = sa.create_engine(url, poolclass=sa.pool.NullPool)
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT is_nullable FROM information_schema.columns"
                    " WHERE table_schema = 'public' AND table_name = 'job_runs'"
                    " AND column_name = 'user_id'"
                )
            ).fetchone()
            return row is not None and row[0] == "NO"
    finally:
        engine.dispose()


@pytest.mark.integration
def test_0008_back_fills_user_id_from_parent_job():
    """Back-fill copies each parent job's user_id into its child job_runs.

    Closes the gap flagged in PR #168: the migration's correctness wasn't
    asserted by any test — only the safety gate ("zero NULLs remain") was
    indirectly exercised by the upgrade succeeding. A buggy back-fill that
    mapped the wrong parent (e.g. wrong join key) would still pass the gate
    and silently land cross-tenant user_ids on runs.
    """
    url = os.environ["ALEMBIC_DATABASE_URL"]

    # Pre-state: revert to 0007 so user_id is nullable again and we can seed
    # NULL rows the migration is supposed to fix up.
    _run_alembic_with_operator("downgrade", "0007")

    # Two parent jobs with distinct owners; one NULL-user run per parent.
    job_a = _insert_job(url, "user-alpha")
    job_b = _insert_job(url, "user-beta")
    run_a = _insert_job_run_null_user(url, job_a)
    run_b = _insert_job_run_null_user(url, job_b)

    # Sanity: the seeded runs really are NULL pre-migration.
    assert _get_run_user(url, run_a) is None
    assert _get_run_user(url, run_b) is None

    # Apply 0008 — back-fills, gates on zero NULLs remaining, then sets NOT NULL.
    _run_alembic_with_operator("upgrade", "0008")

    # Each run now carries its own parent's user_id — not swapped, not
    # collapsed onto a single owner.
    assert _get_run_user(url, run_a) == "user-alpha"
    assert _get_run_user(url, run_b) == "user-beta"

    # And the constraint actually tightened — without this assert, a no-op
    # ALTER could fool the test.
    assert _user_id_is_not_null(url), "user_id must be NOT NULL after 0008"

    # Restore head for subsequent tests.
    _run_alembic_with_operator("upgrade", "head")


@pytest.mark.integration
def test_0006_creates_and_drops_oauth_connections():
    """Migration 0006 creates oauth_connections; downgrade drops it."""
    url = os.environ["ALEMBIC_DATABASE_URL"]
    engine = sa.create_engine(url)

    _run_alembic_with_operator("upgrade", "0006")
    with engine.connect() as conn:
        tables = sa.inspect(conn).get_table_names()
    assert "oauth_connections" in tables, "oauth_connections must exist after 0006 upgrade"

    _run_alembic_with_operator("downgrade", "0005")
    with engine.connect() as conn:
        tables_after = sa.inspect(conn).get_table_names()
    assert "oauth_connections" not in tables_after, "oauth_connections must be gone after downgrade"

    _run_alembic_with_operator("upgrade", "head")
    engine.dispose()
