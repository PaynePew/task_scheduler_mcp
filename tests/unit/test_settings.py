from app.config.settings import Settings


def test_settings_defaults():
    s = Settings()
    assert s.log_level == "INFO"
    assert s.mcp_user_id == "default-user"
    assert s.port == 8000
    assert "asyncpg" in s.database_url
    assert "psycopg" in s.alembic_database_url
    assert "9324" in s.queue_url


def test_git_sha_default():
    s = Settings()
    assert s.git_sha == "unknown"


def test_git_sha_loaded_from_env(monkeypatch):
    monkeypatch.setenv("GIT_SHA", "deadbeef1234567")
    s = Settings()
    assert s.git_sha == "deadbeef1234567"


def test_reconciler_grace_values_match_documented_derivation():
    """Guards against the 30s/60s visibility drift regressing (issue #270).

    The worker's actual per-receive VisibilityTimeout is 60s (not the SQS
    main queue's 30s default) — both graces must be derived from that figure.
    """
    s = Settings()
    visibility_timeout_seconds = 60
    max_receive_count = 3

    dlq_slack_seconds = 60
    assert s.reconciler_dlq_grace_seconds == (
        visibility_timeout_seconds * (max_receive_count + 1) + dlq_slack_seconds
    )

    queued_slack_seconds = 30
    assert s.reconciler_queued_grace_seconds == (
        visibility_timeout_seconds * 2 + queued_slack_seconds
    )


def test_reconciler_interval_settings_driven(monkeypatch):
    monkeypatch.setenv("RECONCILER_INTERVAL_SECONDS", "45")
    s = Settings()
    assert s.reconciler_interval_seconds == 45.0
