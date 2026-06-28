"""Phase 2 Stage A (S-A1): mcp-server serves the static landing page at /.

After the platform edge cutover (ADR-0014), the neutral Caddy routes
`handle / -> reverse_proxy mcp-server:8000` and does NOT mount scheduler's
./static. So GET / must return the landing page from the app itself.

These tests also pin that the catch-all static mount is registered LAST, so it
does not shadow the explicit API routes (/mcp, /healthz, ...).
"""

from __future__ import annotations

from starlette.testclient import TestClient

from app.entrypoints.mcp_http import build_app


def _client() -> TestClient:
    # shed_fn disabled → /healthz/shed is a clean 200 without a real backend.
    # The landing + shed routes never touch the DB, so the default session
    # factory is fine (it is only constructed, never connected, here).
    return TestClient(build_app(shed_fn=lambda: False))


def test_root_serves_landing_page() -> None:
    resp = _client().get("/")
    assert resp.status_code == 200
    assert "Task Scheduler MCP" in resp.text
    assert resp.headers["content-type"].startswith("text/html")


def test_stylesheet_served() -> None:
    resp = _client().get("/style.css")
    assert resp.status_code == 200
    assert "text/css" in resp.headers["content-type"]


def test_static_mount_does_not_shadow_explicit_routes() -> None:
    """The catch-all `/` mount must be LAST so /healthz/shed still wins."""
    resp = _client().get("/healthz/shed")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "shed": False}


def test_unknown_static_path_returns_404() -> None:
    resp = _client().get("/nope-does-not-exist.txt")
    assert resp.status_code == 404
