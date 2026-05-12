"""Shared pytest fixtures."""

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.engine import create_async_engine


@pytest_asyncio.fixture
async def async_session() -> AsyncSession:
    """Per-test engine + session with auto-rollback.

    Creating a fresh engine per test avoids cross-event-loop pool reuse —
    pytest-asyncio's default function scope gives each test its own loop,
    and asyncpg connection pools are loop-bound. The module-level
    `async_session_factory` in `app/db/engine.py` is safe for runtime
    services (one engine per process / one loop per process) but unsafe
    for tests where many loops share a process.
    """
    engine = create_async_engine()
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            async with session.begin():
                yield session
                await session.rollback()
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def sqs_client():
    """SQSClient wrapper pointed at the Compose ElasticMQ instance (integration tests only)."""
    from app.queue.sqs import SQSClient

    return SQSClient()
