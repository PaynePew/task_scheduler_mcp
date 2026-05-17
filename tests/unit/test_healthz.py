"""Unit tests for GET /healthz on the MCP HTTP server.

Uses mocked async session factories so no real Postgres is needed.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import httpx
import pytest

from app.config.settings import settings
from app.entrypoints.mcp_http import build_app


def _make_factory(*, raise_exc: Exception | None = None):
    """Return a mock async_sessionmaker-compatible factory."""
    session = AsyncMock()
    if raise_exc is not None:
        session.execute = AsyncMock(side_effect=raise_exc)
    else:
        session.execute = AsyncMock(return_value=None)

    @asynccontextmanager
    async def _factory():
        yield session

    return _factory


@pytest.fixture
def client_ok(monkeypatch):
    monkeypatch.setattr(settings, "git_sha", "abc1234")
    app = build_app(json_response=True, session_factory=_make_factory())
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.fixture
def client_db_down(monkeypatch):
    monkeypatch.setattr(settings, "git_sha", "unknown")
    err = OSError("connection refused")
    app = build_app(json_response=True, session_factory=_make_factory(raise_exc=err))
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_healthz_happy_path(client_ok):
    async with client_ok as client:
        resp = await client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["version"] == "abc1234"
    assert body["db"] == "connected"


async def test_healthz_db_down_returns_503(client_db_down):
    async with client_db_down as client:
        resp = await client.get("/healthz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["ok"] is False
    assert body["db"] == "OSError"


async def test_healthz_timeout_returns_503(monkeypatch):
    monkeypatch.setattr(settings, "git_sha", "unknown")

    async def _slow_execute(*_args, **_kwargs):
        await asyncio.sleep(10)

    session = AsyncMock()
    session.execute = _slow_execute

    @asynccontextmanager
    async def _slow_factory():
        yield session

    app = build_app(json_response=True, session_factory=_slow_factory)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/healthz")
    assert resp.status_code == 503
    assert resp.json()["ok"] is False


async def test_healthz_version_defaults_to_unknown(monkeypatch):
    monkeypatch.setattr(settings, "git_sha", "unknown")
    app = build_app(json_response=True, session_factory=_make_factory())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["version"] == "unknown"
