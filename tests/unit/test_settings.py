from app.config.settings import Settings


def test_settings_defaults():
    s = Settings()
    assert s.log_level == "INFO"
    assert s.mcp_user_id == "default-user"
    assert s.port == 8000
    assert "asyncpg" in s.database_url
    assert "psycopg" in s.alembic_database_url
    assert "9324" in s.queue_url
