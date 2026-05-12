"""Worker process entrypoint.

Pool sized 5+10 per ADR-011 (worker role).
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.engine import create_async_engine
from app.queue.sqs import SQSClient
from app.workers.executor import run_worker

logging.basicConfig(level="INFO", format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


async def _main() -> None:
    # ADR-011: worker pool = 5 + 10
    engine = create_async_engine(pool_size=5, max_overflow=10)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    sqs = SQSClient()
    try:
        await run_worker(session_factory, sqs)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_main())
