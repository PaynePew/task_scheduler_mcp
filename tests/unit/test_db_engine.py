"""Unit tests for app/db/engine.py — verifies exports and configuration without a DB."""

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.db.engine import async_session_factory, create_async_engine


def test_create_async_engine_returns_async_engine():
    engine = create_async_engine()
    assert isinstance(engine, AsyncEngine)
    engine.sync_engine.dispose()


def test_engine_uses_asyncpg_dialect():
    engine = create_async_engine()
    assert engine.dialect.name == "postgresql"
    assert "asyncpg" in str(engine.url)
    engine.sync_engine.dispose()


def test_async_session_factory_is_async_sessionmaker():
    assert isinstance(async_session_factory, async_sessionmaker)


def test_async_session_factory_expire_on_commit_false():
    # expire_on_commit=False is required per ADR-011 to avoid DetachedInstanceError
    assert async_session_factory.kw.get("expire_on_commit") is False


def test_engine_pool_settings_respected():
    from app.config.settings import settings

    engine = create_async_engine()
    pool = engine.sync_engine.pool
    assert pool.size() == settings.db_pool_size
    engine.sync_engine.dispose()
