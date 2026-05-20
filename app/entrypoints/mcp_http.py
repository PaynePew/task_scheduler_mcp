"""HTTP entrypoint for the MCP server — Streamable HTTP transport per ADR-006/015.

X-User-Id header → MCP_USER_ID env → "default-user" resolver chain per ADR-015.
Trust-only multi-tenancy; no JWT/signature validation (documented W1 limitation).

Overload protection (ADR-057) applied in this order before invoking MCP handlers:
  1. Load shedding  — should_shed() → 503 + Retry-After
  2. Rate limiting  — DB-backed per-user limiter → 429 + Retry-After
  3. Concurrency    — in-flight semaphore → 503
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import Callable

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
from app.overload.health import CapacityExceeded, ConcurrencyLimiter, should_shed
from app.ratelimit.checker import Allow, RateLimits, check_rate_limit

logger = logging.getLogger(__name__)


class _McpHttpEndpoint:
    """Per-request stateless MCP handler with overload protection.

    Creates a fresh MCP server instance for each request so that the user_id
    resolved from the X-User-Id header is bound for the lifetime of that request.
    No shared state between requests → hard tenant isolation without sessions.

    Overload checks applied before MCP server invocation (ADR-057):
      1. Load shedding (should_shed) → 503
      2. Rate limiting (DB-backed)   → 429
      3. Concurrency cap (semaphore) → 503
    """

    def __init__(
        self,
        json_response: bool = False,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        limiter: ConcurrencyLimiter | None = None,
        shed_fn: Callable[[], bool] | None = None,
    ) -> None:
        self._json_response = json_response
        self._session_factory = session_factory or _default_session_factory
        self._limiter = limiter or ConcurrencyLimiter(settings.overload_concurrency_limit)
        self._shed_fn = shed_fn if shed_fn is not None else should_shed

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return

        request = Request(scope, receive)

        # ------------------------------------------------------------------
        # 1. Load shedding — health-based; before any business logic
        # ------------------------------------------------------------------
        if self._shed_fn():
            resp = JSONResponse(
                {
                    "ok": False,
                    "error": {
                        "code": "OVERLOADED",
                        "message": "Service is temporarily overloaded. Please retry later.",
                    },
                },
                status_code=503,
                headers={"Retry-After": str(settings.overload_retry_after_seconds)},
            )
            await resp(scope, receive, send)
            return

        # ------------------------------------------------------------------
        # 2. Rate limiting — per-user, DB-backed; returns 429 + Retry-After
        # ------------------------------------------------------------------
        x_user_id = request.headers.get("x-user-id")
        user_id = resolve_user_id(x_user_id)

        limits = RateLimits(
            daily=settings.rate_limit_daily,
            burst=settings.rate_limit_burst_per_minute,
        )
        async with self._session_factory() as rl_session:
            decision = await check_rate_limit(user_id, rl_session, limits)
        if not isinstance(decision, Allow):
            resp = JSONResponse(
                {
                    "ok": False,
                    "error": {
                        "code": "RATE_LIMITED",
                        "message": f"Rate limit exceeded ({decision.reason}). "
                        f"Retry after {decision.retry_after_seconds} seconds.",
                        "retry_after_seconds": decision.retry_after_seconds,
                    },
                },
                status_code=429,
                headers={"Retry-After": str(decision.retry_after_seconds)},
            )
            await resp(scope, receive, send)
            return

        # ------------------------------------------------------------------
        # 3. Concurrency limiter — in-flight cap; returns 503 when full
        # ------------------------------------------------------------------
        try:
            async with self._limiter.acquire():
                await self._handle_mcp(scope, receive, send, user_id)
        except CapacityExceeded:
            resp = JSONResponse(
                {
                    "ok": False,
                    "error": {
                        "code": "OVERLOADED",
                        "message": "Too many concurrent requests. Please retry later.",
                    },
                },
                status_code=503,
                headers={"Retry-After": str(settings.overload_retry_after_seconds)},
            )
            await resp(scope, receive, send)

    async def _handle_mcp(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        user_id: str,
    ) -> None:
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
      - 200  {"ok": true, "version": "<git sha or 'unknown'>", "db": "connected"}
      - 503  {"ok": false, "db": "<exception class name>"}
    """
    factory = session_factory or _default_session_factory

    async def _healthz(_request: Request) -> JSONResponse:
        try:
            async with factory() as session:
                await asyncio.wait_for(session.execute(text("SELECT 1")), timeout=0.5)
            return JSONResponse({"ok": True, "version": settings.git_sha, "db": "connected"})
        except Exception as exc:
            logger.warning("healthz db probe failed: %s", type(exc).__name__)
            return JSONResponse({"ok": False, "db": type(exc).__name__}, status_code=503)

    return _healthz


def _make_shed_endpoint(shed_fn: Callable[[], bool] | None = None):
    """Return a Starlette endpoint for GET /healthz/shed.

    Caddy uses this as a health-check URI (ADR-057 AC2): when the endpoint
    returns 503, Caddy marks the backend as unavailable and returns 503 to
    clients directly, shedding load at the edge before it reaches mcp-server.

    - 200  {"ok": true, "shed": false}  — backend is healthy, accept traffic
    - 503  {"ok": false, "shed": true}  — backend is overloaded, shed traffic
    """
    fn = shed_fn if shed_fn is not None else should_shed

    async def _shed(_request: Request) -> JSONResponse:
        if fn():
            return JSONResponse(
                {"ok": False, "shed": True},
                status_code=503,
                headers={"Retry-After": str(settings.overload_retry_after_seconds)},
            )
        return JSONResponse({"ok": True, "shed": False})

    return _shed


def build_app(
    json_response: bool = False,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    limiter: ConcurrencyLimiter | None = None,
    shed_fn: Callable[[], bool] | None = None,
) -> Starlette:
    """Return the Starlette ASGI app for the HTTP MCP transport.

    Args:
        json_response: When True, return plain JSON instead of SSE streams.
            Useful for testing; production default is False (SSE).
        session_factory: Injectable DB session factory.  Tests pass a per-test
            fresh factory to avoid asyncpg cross-event-loop errors; omit in
            production (falls back to the module-level factory).
        limiter: Injectable ConcurrencyLimiter.  Tests can pass a pre-sized
            limiter; omit in production (uses settings.overload_concurrency_limit).
        shed_fn: Injectable load-shedding predicate.  Tests can pass a stub
            (e.g. ``lambda: False``) to disable shedding; omit in production.
    """
    handler = _McpHttpEndpoint(
        json_response=json_response,
        session_factory=session_factory,
        limiter=limiter,
        shed_fn=shed_fn,
    )
    healthz = _make_healthz_endpoint(session_factory=session_factory)
    shed = _make_shed_endpoint(shed_fn=shed_fn)
    return Starlette(
        routes=[
            Route("/mcp", endpoint=handler, methods=["GET", "POST", "DELETE"]),
            Route("/healthz", endpoint=healthz, methods=["GET"]),
            Route("/healthz/shed", endpoint=shed, methods=["GET"]),
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
