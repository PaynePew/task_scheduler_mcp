"""Shared pytest fixtures."""

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import async_session_factory


@pytest_asyncio.fixture
async def async_session() -> AsyncSession:
    """Yield an AsyncSession with automatic rollback at teardown."""
    async with async_session_factory() as session:
        async with session.begin():
            yield session
            await session.rollback()


@pytest.fixture(scope="session")
def sqs_client():
    """Boto3 SQS client pointed at the Compose ElasticMQ instance.

    TODO (S06): activate once the queue layer is wired.
    """
    pytest.skip("sqs_client not available until S06 wires the queue layer")
    yield  # pragma: no cover
