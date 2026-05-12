"""Shared pytest fixtures.

async_session and sqs_client are stubbed out here so S02/S06 can activate them
without changing the fixture interface. Tests that use these fixtures will be
skipped until the underlying infrastructure is wired.
"""

import pytest


@pytest.fixture
async def async_session():
    """Yield an AsyncSession with automatic rollback at teardown.

    TODO (S02): activate once app/db/engine.py exists.
    """
    pytest.skip("async_session not available until S02 lands app/db/engine.py")
    yield  # pragma: no cover


@pytest.fixture(scope="session")
def sqs_client():
    """Boto3 SQS client pointed at the Compose ElasticMQ instance.

    TODO (S06): activate once the queue layer is wired.
    """
    pytest.skip("sqs_client not available until S06 wires the queue layer")
    yield  # pragma: no cover
