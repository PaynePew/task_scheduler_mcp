from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)
from sqlalchemy.ext.asyncio import (
    create_async_engine as _sa_create_async_engine,
)

from app.config.settings import settings


def create_async_engine() -> AsyncEngine:
    """Construct the asyncpg engine with pool settings from config per ADR-011."""
    return _sa_create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_recycle=3600,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
    )


# Module-level engine and session factory — import these in service code
_engine: AsyncEngine = create_async_engine()

async_session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    _engine,
    expire_on_commit=False,
)
