"""Static landing page served by mcp-server (Phase 2 Stage A / S-A1, ADR-0014).

Before the edge cutover the landing page was served by Caddy's ``file_server``
off a ``./static`` bind-mount. The platform-owned **edge** Caddy is pure
``reverse_proxy`` and deliberately does NOT mount scheduler's static, so the app
must serve ``/`` itself. The edge Caddyfile already routes
``handle / -> reverse_proxy mcp-server:8000``; once the app serves ``/`` the
cutover is transparent.

The assets live in ``app/web/static/`` so they ship in the image via the
Dockerfile's ``COPY app/ ./app/`` (no extra COPY, no env var). The mount MUST be
registered LAST in the route list so the explicit API routes (/mcp, /healthz,
/healthz/shed, /.well-known/*, /connections*) win; this catch-all only serves
``/``, ``/style.css`` and any other static asset.
"""

from __future__ import annotations

from pathlib import Path

from starlette.routing import Mount
from starlette.staticfiles import StaticFiles

STATIC_DIR = Path(__file__).resolve().parent / "static"


def make_landing_mount() -> Mount:
    """Return the catch-all static mount for the landing page. Register LAST."""
    return Mount("/", app=StaticFiles(directory=STATIC_DIR, html=True), name="landing")
