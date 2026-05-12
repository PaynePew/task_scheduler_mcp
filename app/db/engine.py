from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)
from sqlalchemy.ext.asyncio import (
    create_async_engine as _sa_create_async_engine,
)

from app.config.settings import settings


def create_async_engine(
    pool_size: int | None = None,
    max_overflow: int | None = None,
) -> AsyncEngine:
    """Construct the asyncpg engine with pool settings from config per ADR-011.

    Pass pool_size / max_overflow to override the settings defaults — used by
    process-role entrypoints (e.g. watcher: 2+3, mcp-server: 5+10).
    """
    return _sa_create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_recycle=3600,
        pool_size=pool_size if pool_size is not None else settings.db_pool_size,
        max_overflow=max_overflow if max_overflow is not None else settings.db_max_overflow,
    )


# Module-level engine and session factory — import these in service code
_engine: AsyncEngine = create_async_engine()

async_session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    _engine,
    expire_on_commit=False,
)
