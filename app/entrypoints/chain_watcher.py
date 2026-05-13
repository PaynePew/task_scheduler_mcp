"""ChainWatcher process entrypoint.

Pool sized 2+3 per ADR-011 (chain-watcher role).
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.engine import create_async_engine
from app.workers.chain_watcher import run_chain_watcher

logging.basicConfig(level="INFO", format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


async def _main() -> None:
    # ADR-011: chain-watcher pool = 2 + 3
    engine = create_async_engine(pool_size=2, max_overflow=3)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await run_chain_watcher(session_factory)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_main())
