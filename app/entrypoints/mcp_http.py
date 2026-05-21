"""HTTP entrypoint for the MCP server — Streamable HTTP transport (ADR-006/049/053/057).

OAuth 2.1 resource-server auth per ADR-053:
  - Every /mcp request requires a valid WorkOS-issued Bearer token.
  - Unauthenticated / invalid-token requests → 401 + WWW-Authenticate.
  - X-User-Id header is ignored; user_id comes from the verified JWT sub.
  - Protected Resource Metadata served at /.well-known/oauth-protected-resource.

When WORKOS_JWKS_URI / WORKOS_ISSUER / WORKOS_AUDIENCE are all unset the server
falls back to trust-only mode (X-User-Id header → MCP_USER_ID env → "default-user"
per ADR-015) for local dev and CI runs that don't have WorkOS credentials.

Overload protection (ADR-057) applied in this order before invoking MCP handlers:
  1. Load shedding  — should_shed() → 503 + Retry-After  (before auth, cheap)
  2. Auth           — Bearer JWT or trust-only fallback → 401 on failure
  3. Rate limiting  — DB-backed per-user limiter → 429 + Retry-After
  4. Concurrency    — in-flight semaphore → 503
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

from app.auth.token_validation import TokenValidationError, validate_token
from app.config.settings import settings
from app.db.engine import async_session_factory as _default_session_factory
from app.db.identity import resolve_user_id_stdio
from app.mcp.server import create_server
from app.obs.logging import bind_user_id, configure_logging, unbind_user_id
from app.overload.health import CapacityExceeded, ConcurrencyLimiter, should_shed
from app.ratelimit.checker import Allow, RateLimits, check_rate_limit
from app.web.connections import make_routes as make_connections_routes

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

_AUTH_ENABLED = bool(
    settings.workos_jwks_uri and settings.workos_issuer and settings.workos_audience
)
if not _AUTH_ENABLED:
    logger.info(
        "WorkOS auth disabled: WORKOS_JWKS_URI / WORKOS_ISSUER / WORKOS_AUDIENCE not set. "
        "Running in trust-only mode (X-User-Id header accepted as authoritative). "
        "Set all three env vars for production auth."
    )


def _www_authenticate_header() -> str:
    """Build the WWW-Authenticate value pointing at WorkOS (RFC 6750 / RFC 9728)."""
    parts = ['Bearer realm="task-scheduler-mcp"']
    if settings.workos_issuer:
        parts.append(
            f'resource_metadata="{settings.workos_issuer}/.well-known/oauth-protected-resource"'
        )
    return ", ".join(parts)


def _extract_bearer(request: Request) -> str | None:
    """Return the raw Bearer token from the Authorization header, or None."""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    return None


def _resolve_user_id(request: Request) -> tuple[str, None] | tuple[None, JSONResponse]:
    """Return (user_id, None) on success or (None, error_response) on failure.

    Resolution order per ADR-053 / ADR-049:
      - Auth enabled (WorkOS configured): Bearer JWT → sub claim.
        X-User-Id is explicitly ignored in this mode.
      - Auth disabled (no WorkOS creds, local dev / CI): X-User-Id header →
        MCP_USER_ID env → "default-user" (old trust-only chain, per ADR-015).
    """
    if not _AUTH_ENABLED:
        x_user_id = request.headers.get("x-user-id") or None
        if x_user_id:
            return x_user_id, None
        return resolve_user_id_stdio(), None

    token = _extract_bearer(request)
    if token is None:
        resp = JSONResponse(
            {"error": "unauthorized", "detail": "Bearer token required"},
            status_code=401,
            headers={"WWW-Authenticate": _www_authenticate_header()},
        )
        return None, resp

    # _AUTH_ENABLED gates this branch on all three being non-None, but the
    # checker can't narrow through the module-level constant — hence the
    # explicit type: ignore on each argument.
    try:
        ctx = validate_token(
            token,
            issuer=settings.workos_issuer,  # type: ignore[arg-type]
            audience=settings.workos_audience,  # type: ignore[arg-type]
            jwks_uri=settings.workos_jwks_uri,  # type: ignore[arg-type]
        )
    except TokenValidationError as exc:
        logger.debug("Token validation failed: %s", exc)
        resp = JSONResponse(
            {"error": "unauthorized", "detail": str(exc)},
            status_code=401,
            headers={"WWW-Authenticate": _www_authenticate_header()},
        )
        return None, resp

    return ctx.user_id, None


# ---------------------------------------------------------------------------
# MCP handler
# ---------------------------------------------------------------------------


class _McpHttpEndpoint:
    """Per-request stateless MCP handler with OAuth 2.1 auth + overload protection.

    Creates a fresh MCP server instance per request with the verified user_id
    bound for the lifetime of that request.  No shared state between requests →
    hard tenant isolation without sessions.

    Checks applied before MCP server invocation (ADR-053 + ADR-057):
      1. Load shedding (should_shed) → 503  (before auth, cheap)
      2. Auth (_resolve_user_id)     → 401
      3. Rate limiting (DB-backed)   → 429
      4. Concurrency cap (semaphore) → 503
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
        # 1. Load shedding — health-based; before auth so we can drop load
        #    even for unauthenticated requests when overloaded.
        # ------------------------------------------------------------------
        if self._shed_fn():
            resp = JSONResponse(
                {
                    "ok": False,
                    "error": {
                        "code": "INVALID_STATE",
                        "message": "Service is temporarily overloaded. Please retry later.",
                    },
                },
                status_code=503,
                headers={"Retry-After": str(settings.overload_retry_after_seconds)},
            )
            await resp(scope, receive, send)
            return

        # ------------------------------------------------------------------
        # 2. Auth — Bearer JWT (when WorkOS is configured) or trust-only
        #    fallback. Returns 401 + WWW-Authenticate on failure.
        # ------------------------------------------------------------------
        user_id, err_response = _resolve_user_id(request)
        if err_response is not None:
            await err_response(scope, receive, send)
            return

        # ------------------------------------------------------------------
        # 3. Rate limiting — per-user, DB-backed; returns 429 + Retry-After
        # ------------------------------------------------------------------
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
                        "code": "INVALID_STATE",
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
        # 4. Concurrency limiter — in-flight cap; returns 503 when full
        # ------------------------------------------------------------------
        try:
            async with self._limiter.acquire():
                await self._handle_mcp(scope, receive, send, user_id)
        except CapacityExceeded:
            resp = JSONResponse(
                {
                    "ok": False,
                    "error": {
                        "code": "INVALID_STATE",
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
        tok = bind_user_id(user_id)
        try:
            server = create_server(user_id, session_factory=self._session_factory)
            transport = StreamableHTTPServerTransport(
                mcp_session_id=None,
                is_json_response_enabled=self._json_response,
            )

            async def _run_server(
                *, task_status: TaskStatus[None] = anyio.TASK_STATUS_IGNORED
            ) -> None:
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
        finally:
            unbind_user_id(tok)


# ---------------------------------------------------------------------------
# Protected Resource Metadata (RFC 9728)
# ---------------------------------------------------------------------------


def _make_prm_endpoint() -> Route:
    """Return the /.well-known/oauth-protected-resource route (RFC 9728)."""

    async def _prm(_request: Request) -> JSONResponse:
        authorization_servers = [settings.workos_issuer] if settings.workos_issuer else []
        body: dict[str, object] = {
            "resource": settings.workos_audience or "task-scheduler-mcp",
            "authorization_servers": authorization_servers,
            "bearer_methods_supported": ["header"],
            "resource_documentation": "https://github.com/PaynePew/task_scheduler_mcp",
        }
        return JSONResponse(body)

    return Route("/.well-known/oauth-protected-resource", endpoint=_prm, methods=["GET"])


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


def _make_healthz_endpoint(
    session_factory: async_sessionmaker[AsyncSession] | None = None,
):
    """Return a Starlette endpoint function for GET /healthz."""
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


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


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
    factory = session_factory or _default_session_factory
    connections_routes = make_connections_routes(factory)
    return Starlette(
        routes=[
            Route("/mcp", endpoint=handler, methods=["GET", "POST", "DELETE"]),
            Route("/healthz", endpoint=healthz, methods=["GET"]),
            Route("/healthz/shed", endpoint=shed, methods=["GET"]),
            _make_prm_endpoint(),
            *connections_routes,
        ],
    )


# ---------------------------------------------------------------------------
# CLI entry points
# ---------------------------------------------------------------------------


def _run_http() -> None:
    import uvicorn

    configure_logging("mcp-http")
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
