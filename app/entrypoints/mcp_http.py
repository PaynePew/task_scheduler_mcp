"""HTTP entrypoint for the MCP server — Streamable HTTP transport per ADR-006/015.

X-User-Id header → MCP_USER_ID env → "default-user" resolver chain per ADR-015.
Trust-only multi-tenancy; no JWT/signature validation (documented W1 limitation).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os

import anyio
from anyio.abc import TaskStatus
from mcp.server.streamable_http import StreamableHTTPServerTransport
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from app.config.settings import settings
from app.db.engine import async_session_factory as _default_session_factory
from app.db.identity import resolve_user_id
from app.mcp.server import create_server

logger = logging.getLogger(__name__)


class _McpHttpEndpoint:
    """Per-request stateless MCP handler.

    Creates a fresh MCP server instance for each request so that the user_id
    resolved from the X-User-Id header is bound for the lifetime of that request.
    No shared state between requests → hard tenant isolation without sessions.
    """

    def __init__(
        self,
        json_response: bool = False,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self._json_response = json_response
        self._session_factory = session_factory

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return

        request = Request(scope, receive)
        x_user_id = request.headers.get("x-user-id")
        user_id = resolve_user_id(x_user_id)

        server = create_server(user_id, session_factory=self._session_factory)
        transport = StreamableHTTPServerTransport(
            mcp_session_id=None,
            is_json_response_enabled=self._json_response,
        )

        async def _run_server(*, task_status: TaskStatus[None] = anyio.TASK_STATUS_IGNORED) -> None:
            async with transport.connect() as streams:
                read_stream, write_stream = streams
                task_status.started()
                try:
                    await server.run(
                        read_stream,
                        write_stream,
                        server.create_initialization_options(),
                        stateless=True,
                    )
                except Exception:
                    logger.exception("Stateless MCP server crashed for user %s", user_id)

        async with anyio.create_task_group() as tg:
            await tg.start(_run_server)
            await transport.handle_request(scope, receive, send)
            await transport.terminate()
            tg.cancel_scope.cancel()


def _make_healthz_endpoint(
    session_factory: async_sessionmaker[AsyncSession] | None = None,
):
    """Return a Starlette endpoint function for GET /healthz.

    Probes Postgres with a cheap SELECT 1 (500 ms timeout) and returns:
      - 200  {"ok": true, "version": "<GIT_SHA or 'unknown'>", "db": "connected"}
      - 503  {"ok": false, "db": "<exception class name>"}
    """
    _factory = session_factory or _default_session_factory

    async def _healthz(request: Request) -> JSONResponse:
        version = os.environ.get("GIT_SHA", "unknown")
        try:
            async with _factory() as session:
                await asyncio.wait_for(session.execute(text("SELECT 1")), timeout=0.5)
            return JSONResponse({"ok": True, "version": version, "db": "connected"})
        except Exception as exc:
            return JSONResponse({"ok": False, "db": type(exc).__name__}, status_code=503)

    return _healthz


def build_app(
    json_response: bool = False,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> Starlette:
    """Return the Starlette ASGI app for the HTTP MCP transport.

    Args:
        json_response: When True, return plain JSON instead of SSE streams.
            Useful for testing; production default is False (SSE).
        session_factory: Injectable DB session factory.  Tests pass a per-test
            fresh factory to avoid asyncpg cross-event-loop errors; omit in
            production (falls back to the module-level factory).
    """
    handler = _McpHttpEndpoint(json_response=json_response, session_factory=session_factory)
    healthz = _make_healthz_endpoint(session_factory=session_factory)
    return Starlette(
        routes=[
            Route("/mcp", endpoint=handler, methods=["GET", "POST", "DELETE"]),
            Route("/healthz", endpoint=healthz, methods=["GET"]),
        ],
    )


# ---------------------------------------------------------------------------
# CLI entry points
# ---------------------------------------------------------------------------


def _run_http() -> None:
    import uvicorn

    logging.basicConfig(level=getattr(logging, settings.log_level.upper()))
    app = build_app()
    uvicorn.run(app, host="0.0.0.0", port=settings.port)


def _run_stdio() -> None:
    from app.entrypoints.mcp_stdio import main as stdio_main

    asyncio.run(stdio_main())


def main() -> None:
    """Unified entrypoint: --transport http (default) or --transport stdio."""
    parser = argparse.ArgumentParser(description="MCP Task Scheduler server")
    parser.add_argument(
        "--transport",
        choices=["http", "stdio"],
        default="http",
        help="Transport to use: 'http' (Streamable HTTP, default) or 'stdio'",
    )
    args = parser.parse_args()
    if args.transport == "stdio":
        _run_stdio()
    else:
        _run_http()


if __name__ == "__main__":
    main()
