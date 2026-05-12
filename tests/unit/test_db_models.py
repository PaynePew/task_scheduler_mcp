"""Unit tests for app/db/models.py — verifies ORM model structure without a DB."""

from sqlalchemy import inspect

from app.db.models import Base, Job, JobRun, RunEvent


def _col_names(model_class):
    mapper = inspect(model_class)
    return {c.key for c in mapper.columns}


def test_job_table_name():
    assert Job.__tablename__ == "jobs"


def test_job_run_table_name():
    assert JobRun.__tablename__ == "job_runs"


def test_run_event_table_name():
    assert RunEvent.__tablename__ == "run_events"


def test_job_has_idempotency_key_user_scoped_unique():
    # Uniqueness is on (user_id, idempotency_key) — not column-level — so two users
    # can share an idempotency key (per CONTEXT.md §5 + ADR-015).
    constraints = {c.name for c in Job.__table__.constraints}
    assert "uq_jobs_user_idempotency" in constraints


def test_job_has_w2_bonus_columns():
    cols = _col_names(Job)
    bonus_cols = (
        "cron_expr",
        "trigger_on_job_id",
        "trigger_on_status",
        "raw_user_input",
        "parsing_metadata",
    )
    for col in bonus_cols:
        assert col in cols, f"Missing W2 bonus column: {col}"


def test_job_run_has_time_bucket_and_composite_pk():
    pks = {col.name for col in JobRun.__table__.primary_key.columns}
    assert pks == {"time_bucket", "run_id"}


def test_job_run_has_wait_for_run_id():
    assert "wait_for_run_id" in _col_names(JobRun)


def test_run_event_processed_by_is_jsonb():
    col = RunEvent.__table__.c["processed_by"]
    from sqlalchemy.dialects.postgresql import JSONB

    assert isinstance(col.type, JSONB)


def test_all_three_tables_in_metadata():
    table_names = set(Base.metadata.tables.keys())
    assert {"jobs", "job_runs", "run_events"} <= table_names


def test_idx_job_runs_due_partial_index_exists():
    indexes = {idx.name for idx in JobRun.__table__.indexes}
    assert "idx_job_runs_due" in indexes


def test_idx_jobs_user_created_exists():
    indexes = {idx.name for idx in Job.__table__.indexes}
    assert "idx_jobs_user_created" in indexes
