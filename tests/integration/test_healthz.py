"""Integration test: GET /healthz against a real Postgres connection.

Mirrors the W2 pattern from tests/integration/test_http_transport.py.
The app is started in-process via httpx.ASGITransport; no real TCP port needed.
"""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.engine import create_async_engine
from app.entrypoints.mcp_http import build_app


@pytest_asyncio.fixture
async def session_factory() -> async_sessionmaker[AsyncSession]:
    """Fresh engine per test; disposes on teardown."""
    engine = create_async_engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture
def healthz_client(session_factory) -> httpx.AsyncClient:
    app = build_app(json_response=True, session_factory=session_factory)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.integration
async def test_healthz_ok_with_real_db(healthz_client, monkeypatch):
    monkeypatch.setenv("GIT_SHA", "deadbeef")
    async with healthz_client as client:
        resp = await client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["version"] == "deadbeef"
    assert body["db"] == "connected"
