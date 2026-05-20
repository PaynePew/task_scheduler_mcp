"""Watcher process entrypoint.

Pool sized 2+3 per ADR-011 (watcher role).
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.engine import create_async_engine
from app.obs.logging import configure_logging
from app.queue.sqs import SQSClient
from app.workers.watcher import run_watcher

configure_logging("watcher")
logger = logging.getLogger(__name__)


async def _main() -> None:
    # ADR-011: watcher pool = 2 + 3
    engine = create_async_engine(pool_size=2, max_overflow=3)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    sqs = SQSClient()
    try:
        await run_watcher(session_factory, sqs)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_main())
