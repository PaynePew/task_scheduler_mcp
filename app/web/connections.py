"""Web routes for the /connections dashboard and OAuth flows (ADR-058).

Routes:
  GET  /connections                  — dashboard (requires session cookie)
  GET  /connections/login            — initiate WorkOS login (or trust-only for dev)
  GET  /connections/auth/callback    — WorkOS authorization code callback
  GET  /connections/github/connect   — start GitHub OAuth
  GET  /connections/github/callback  — GitHub OAuth callback → store token
  POST /connections/github/disconnect — remove GitHub connection
  GET  /connections/slack/connect    — start Slack OAuth
  GET  /connections/slack/callback   — Slack OAuth callback → store token
  POST /connections/slack/disconnect  — remove Slack connection
  GET  /connections/google/connect   — start Google OAuth (Gmail send scope)
  GET  /connections/google/callback  — Google OAuth callback → store token
  POST /connections/google/disconnect — remove Google connection

Session mechanism:
  JWT HS256 cookie named ``session`` signed with ``settings.web_session_secret``.
  Sub claim contains user_id; expiry is 24 hours.

Trust-only mode (when workos_client_id is not set):
  /connections/login sets a session cookie directly using MCP_USER_ID env var
  (or "default-user") so local dev / CI can exercise the dashboard without
  real WorkOS credentials.
"""

from __future__ import annotations

import html
import logging
import secrets
import urllib.parse
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import jwt
from jwt import PyJWKClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from app.auth.token_validation import TokenValidationError, validate_token
from app.config.settings import settings
from app.connections.store import ConnectionMiss, ConnectionStore
from app.crypto.envelope_factory import kms_envelope_from_settings as _make_kms_envelope
from app.db.identity import resolve_user_id_stdio

logger = logging.getLogger(__name__)

_SESSION_COOKIE = "session"
_STATE_COOKIE = "oauth_state"
_SESSION_TTL = timedelta(hours=24)
_GITHUB_OAUTH_SCOPE = "read:user repo"
_SLACK_OAUTH_SCOPE = "chat:write chat:write.public"
_GOOGLE_OAUTH_SCOPE = "https://www.googleapis.com/auth/gmail.send"
_GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"

# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------


def _create_session_token(user_id: str) -> str:
    """Return a signed JWT to store in the session cookie."""
    now = datetime.now(UTC)
    exp = int((now + _SESSION_TTL).timestamp())
    payload = {"sub": user_id, "iat": int(now.timestamp()), "exp": exp}
    return jwt.encode(payload, settings.web_session_secret, algorithm="HS256")


def _decode_session_token(token: str) -> str | None:
    """Return user_id from a valid session token, or None on any failure."""
    try:
        payload = jwt.decode(token, settings.web_session_secret, algorithms=["HS256"])
        return payload.get("sub")
    except jwt.InvalidTokenError:
        return None


def _get_session_user(request: Request) -> str | None:
    """Return the authenticated user_id from the session cookie, or None."""
    token = request.cookies.get(_SESSION_COOKIE)
    if not token:
        return None
    return _decode_session_token(token)


def _set_session(response: Response, user_id: str) -> None:
    """Attach a signed session cookie to *response*."""
    token = _create_session_token(user_id)
    response.set_cookie(
        _SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        max_age=int(_SESSION_TTL.total_seconds()),
        secure=settings.connections_base_url.startswith("https://"),
    )


# ---------------------------------------------------------------------------
# WorkOS id_token verification
# ---------------------------------------------------------------------------


def _verify_id_token_sub(
    id_token: str,
    *,
    issuer: str,
    client_id: str,
    jwks_uri: str,
    _jwks_client: PyJWKClient | None = None,
) -> str:
    """Verify an OIDC id_token signature and return the verified sub claim.

    The id_token audience is the OAuth client_id, per the OIDC spec.  Raises
    TokenValidationError on any verification failure (bad signature, expired,
    wrong audience, missing sub, etc.).

    The *_jwks_client* parameter is injectable so tests can supply a pre-built
    PyJWKClient backed by fixture keys without making network calls.
    """
    ctx = validate_token(
        id_token,
        issuer=issuer,
        audience=client_id,
        jwks_uri=jwks_uri,
        _jwks_client=_jwks_client,
    )
    return ctx.user_id


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

# These pages share the landing page's design system (app/web/static): the
# /style.css design tokens + chrome (.site-header, .brand, .lang, .btn,
# .site-footer), the /owl.svg brand mark, and the SAME EN/zh-Hant language
# toggle (localStorage key "tsmcp-lang", so the choice is shared with the
# landing). Asset URLs are ABSOLUTE (/style.css, /owl.svg) because these pages
# live under /connections — relative paths would resolve to /connections/...

_FONTS = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link rel="stylesheet" href="https://fonts.googleapis.com/css2'
    "?family=Fraunces:opsz,wght@9..144,400;9..144,500;9..144,600;9..144,700"
    "&family=IBM+Plex+Mono:wght@400;500;600"
    '&family=IBM+Plex+Sans:wght@400;500;600&display=swap">'
)

# Connections-specific layout, layered on top of the shared tokens in /style.css.
_CONN_CSS = """
  .conn-main { max-width: 660px; margin: 0 auto; padding: clamp(2.2rem,6vw,4.5rem) 24px 3.5rem; }
  .conn-eyebrow { font-family: var(--font-mono); font-size:.74rem; font-weight:500;
    letter-spacing:.18em; text-transform:uppercase; color: var(--accent-2); margin-bottom:12px; }
  .conn-title { font-family: var(--font-display); font-optical-sizing:auto; font-weight:600;
    font-size: clamp(2rem,5vw,2.7rem); line-height:1.04; letter-spacing:-.02em; color: var(--ink); }
  .conn-lead { margin-top:14px; max-width:44em; color: var(--ink-2); font-size:1.04rem; }
  .conn-id { margin-top:22px; display:flex; align-items:center; flex-wrap:wrap; gap:8px;
    font-family: var(--font-mono); font-size:.82rem; color: var(--ink-3); }
  .conn-id code { background: var(--accent-soft); color: var(--accent-2); padding:3px 8px;
    border-radius:6px; font-size:.8rem; word-break:break-all; }
  .conn-id a { color: var(--accent-2); }
  .conn-card { margin-top:24px; background: var(--card); border:1px solid var(--line);
    border-radius:16px; box-shadow: var(--shadow); overflow:hidden; }
  .provider { display:flex; align-items:center; gap:15px; padding:18px 22px;
    border-top:1px solid var(--line-2); }
  .provider:first-child { border-top:0; }
  .provider-mark { flex:0 0 auto; width:40px; height:40px; display:grid; place-items:center;
    border-radius:11px; background: var(--accent-soft);
    border:1px solid color-mix(in srgb, var(--accent) 22%, var(--line)); color: var(--ink); }
  .provider-mark svg { width:21px; height:21px; display:block; }
  .provider-name { font-weight:600; font-size:1.04rem; color: var(--ink); flex:1 1 auto; }
  .pill { font-family: var(--font-mono); font-size:.68rem; font-weight:500; letter-spacing:.07em;
    text-transform:uppercase; padding:5px 11px; border-radius:999px; white-space:nowrap; }
  .pill-on { color: var(--accent-2); background: var(--accent-soft);
    border:1px solid color-mix(in srgb, var(--accent) 30%, transparent); }
  .pill-off { color: var(--ink-3); background:transparent; border:1px solid var(--line); }
  .provider form { margin:0; }
  .provider .btn { padding:8px 16px; font-size:.88rem; }
  .btn-danger { background:transparent; border-color: var(--line); color: var(--ink-2);
    box-shadow:none; }
  .btn-danger:hover { border-color:#c0533a; color:#c0533a; transform:none; box-shadow:none; }
  @media (prefers-color-scheme: dark){ .btn-danger:hover{ border-color:#e69077; color:#e69077; } }
  .conn-foot { margin-top:20px; font-family: var(--font-mono); font-size:.78rem; line-height:1.65;
    color: var(--ink-3); padding-left:14px; border-left:2px solid var(--accent); max-width:48em; }
  .conn-actions { margin-top:26px; }
  @media (max-width:520px){
    .provider { flex-wrap:wrap; }
    .provider-name { flex:1 0 55%; }
  }
"""

# Provider brand marks (monochrome, currentColor). Single-line: SVG path data is
# whitespace-sensitive, so do NOT reflow across implicit string concatenation.
_SVG_GITHUB = '<svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8Z"/></svg>'  # noqa: E501
_SVG_SLACK = '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M5.042 15.165a2.528 2.528 0 0 1-2.52 2.523A2.528 2.528 0 0 1 0 15.165a2.527 2.527 0 0 1 2.522-2.52h2.52v2.52zM6.313 15.165a2.527 2.527 0 0 1 2.521-2.52 2.527 2.527 0 0 1 2.521 2.52v6.313A2.528 2.528 0 0 1 8.834 24a2.528 2.528 0 0 1-2.521-2.522v-6.313zM8.834 5.042a2.528 2.528 0 0 1-2.521-2.52A2.528 2.528 0 0 1 8.834 0a2.528 2.528 0 0 1 2.521 2.522v2.52H8.834zM8.834 6.313a2.528 2.528 0 0 1 2.521 2.521 2.528 2.528 0 0 1-2.521 2.521H2.522A2.528 2.528 0 0 1 0 8.834a2.528 2.528 0 0 1 2.522-2.521h6.312zM18.956 8.834a2.528 2.528 0 0 1 2.522-2.521A2.528 2.528 0 0 1 24 8.834a2.528 2.528 0 0 1-2.522 2.521h-2.522V8.834zM17.688 8.834a2.528 2.528 0 0 1-2.523 2.521 2.527 2.527 0 0 1-2.52-2.521V2.522A2.527 2.527 0 0 1 15.165 0a2.528 2.528 0 0 1 2.523 2.522v6.312zM15.165 18.956a2.528 2.528 0 0 1 2.523 2.522A2.528 2.528 0 0 1 15.165 24a2.527 2.527 0 0 1-2.52-2.522v-2.522h2.52zM15.165 17.688a2.527 2.527 0 0 1-2.52-2.523 2.526 2.526 0 0 1 2.52-2.52h6.313A2.527 2.527 0 0 1 24 15.165a2.528 2.528 0 0 1-2.522 2.523h-6.313z"/></svg>'  # noqa: E501
_SVG_GOOGLE = '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M12.48 10.92v3.28h7.84c-.24 1.84-.853 3.187-1.787 4.133-1.147 1.147-2.933 2.4-6.053 2.4-4.827 0-8.6-3.893-8.6-8.72s3.773-8.72 8.6-8.72c2.6 0 4.507 1.027 5.907 2.347l2.307-2.307C18.747 1.44 16.133 0 12.48 0 5.867 0 .307 5.387.307 12s5.56 12 12.173 12c3.573 0 6.267-1.173 8.373-3.36 2.16-2.16 2.84-5.213 2.84-7.667 0-.76-.053-1.467-.173-2.053H12.48z"/></svg>'  # noqa: E501

_PROVIDERS = (
    ("GitHub", "github", _SVG_GITHUB),
    ("Slack", "slack", _SVG_SLACK),
    ("Google", "google", _SVG_GOOGLE),
)

_HEADER = """<header class="site-header">
  <a class="brand" href="/" aria-label="Owl Task Scheduler MCP home">
    <img src="/owl.svg" alt="" width="40" height="40" class="brand-mark">
    <span class="brand-name">Owl&nbsp;Task&nbsp;Scheduler<span class="brand-mcp">MCP</span></span>
  </a>
  <nav class="site-nav" aria-label="Primary">
    <a href="/" data-en="Home" data-zh="首頁">Home</a>
    <a href="https://github.com/PaynePew/task_scheduler_mcp">GitHub</a>
  </nav>
  <div class="lang" role="group" aria-label="Language / 語言">
    <button type="button" class="lang-btn" data-lang="en">EN</button>
    <button type="button" class="lang-btn" data-lang="zh">中</button>
  </div>
</header>
"""

_FOOTER = """<footer class="site-footer">
  <div class="wrap footer-wrap">
    <div class="footer-brand">
      <img src="/owl.svg" alt="" width="34" height="34">
      <span>Owl Task Scheduler MCP</span>
    </div>
    <nav class="footer-links" aria-label="Footer">
      <a href="/" data-en="Home" data-zh="首頁">Home</a>
      <a href="https://github.com/PaynePew/task_scheduler_mcp">GitHub</a>
      <a href="https://status.paynepew.dev" data-en="Status" data-zh="狀態">Status</a>
    </nav>
  </div>
</footer>
"""

_LANG_JS = """<script>
(function(){var S="tsmcp-lang";
var nodes=document.querySelectorAll("[data-en][data-zh]");
var btns=document.querySelectorAll(".lang-btn");
function apply(l){nodes.forEach(function(e){var v=e.getAttribute("data-"+l);if(v==null)return;
if(e.tagName==="META"){e.setAttribute("content",v);}else{e.textContent=v;}});
document.documentElement.lang=(l==="zh")?"zh-Hant":"en";
btns.forEach(function(b){var on=b.getAttribute("data-lang")===l;b.classList.toggle("is-active",on);
b.setAttribute("aria-pressed",on?"true":"false");});try{localStorage.setItem(S,l);}catch(e){}}
var saved=null;try{saved=localStorage.getItem(S);}catch(e){}
var initial=saved||(((navigator.language||"").toLowerCase().indexOf("zh")===0)?"zh":"en");
btns.forEach(function(b){b.addEventListener("click",function(){apply(b.getAttribute("data-lang"));});});
apply(initial);})();
</script>"""


def _page_head(title: str) -> str:
    """Shared <head> for connection pages — links the landing design system."""
    return (
        '<!DOCTYPE html>\n<html lang="en">\n<head>\n'
        '<meta charset="UTF-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
        '<meta name="theme-color" content="#F3EADA">\n'
        f"<title>{title}</title>\n"
        '<link rel="icon" type="image/svg+xml" href="/favicon.svg">\n'
        f"{_FONTS}\n"
        '<link rel="stylesheet" href="/style.css">\n'
        f"<style>{_CONN_CSS}</style>\n"
        "</head>\n"
    )


def _render_dashboard(user_id: str, connections: list[str]) -> str:
    """Return the connections dashboard, themed to match the landing page."""
    rows = []
    for name, slug, mark in _PROVIDERS:
        if slug in connections:
            pill = (
                '<span class="pill pill-on" data-en="Connected" data-zh="已連接">Connected</span>'
            )
            action = (
                f'<form method="post" action="/connections/{slug}/disconnect">'
                '<button type="submit" class="btn btn-danger" '
                'data-en="Disconnect" data-zh="中斷連接">Disconnect</button></form>'
            )
        else:
            pill = (
                '<span class="pill pill-off" data-en="Not connected" '
                'data-zh="未連接">Not connected</span>'
            )
            action = (
                f'<a class="btn btn-primary" href="/connections/{slug}/connect" '
                'data-en="Connect" data-zh="連接">Connect</a>'
            )
        rows.append(
            '<div class="provider">'
            f'<span class="provider-mark">{mark}</span>'
            f'<span class="provider-name">{name}</span>{pill}{action}</div>'
        )
    rows_html = "\n".join(rows)
    safe_user = html.escape(user_id)
    return (
        _page_head("Connections · Owl Task Scheduler MCP")
        + "<body>\n"
        + _HEADER
        + '<main class="conn-main">\n'
        '<p class="conn-eyebrow" data-en="Account" data-zh="帳號">Account</p>\n'
        '<h1 class="conn-title" data-en="My Connections" data-zh="我的連線">My Connections</h1>\n'
        '<p class="conn-lead" '
        'data-en="Link the providers your scheduled actions act on. '
        'Connect once, and your tasks run on your behalf." '
        'data-zh="連接你的排程動作會用到的服務。連接一次後，任務就能代你執行。">'
        "Link the providers your scheduled actions act on. "
        "Connect once, and your tasks run on your behalf.</p>\n"
        '<p class="conn-id"><span data-en="Signed in as" data-zh="登入身分">Signed in as</span> '
        f"<code>{safe_user}</code> · "
        '<a href="/connections/logout" data-en="Sign out" data-zh="登出">Sign out</a></p>\n'
        f'<div class="conn-card">\n{rows_html}\n</div>\n'
        '<p class="conn-foot" '
        'data-en="GitHub, Slack and Google authorize over OAuth. Tokens are '
        'encrypted at rest and revoked the moment you disconnect." '
        'data-zh="GitHub、Slack 與 Google 透過 OAuth 授權。權杖加密儲存，'
        '按下「中斷連接」即時撤銷。">'
        "GitHub, Slack and Google authorize over OAuth. Tokens are "
        "encrypted at rest and revoked the moment you disconnect.</p>\n"
        '<div class="conn-actions">'
        '<a class="btn btn-ghost btn-small" href="/" '
        'data-en="← Back to home" data-zh="← 返回首頁">← Back to home</a>'
        "</div>\n"
        "</main>\n" + _FOOTER + _LANG_JS + "\n</body>\n</html>"
    )


def _render_login_page(redirect_url: str) -> str:
    """Themed 'sign in required' page (matches the landing design system)."""
    safe_url = html.escape(redirect_url, quote=True)
    return (
        _page_head("Sign in · Owl Task Scheduler MCP")
        + "<body>\n"
        + _HEADER
        + '<main class="conn-main">\n'
        '<p class="conn-eyebrow" data-en="Account" data-zh="帳號">Account</p>\n'
        '<h1 class="conn-title" data-en="Sign in required" '
        'data-zh="需要登入">Sign in required</h1>\n'
        '<p class="conn-lead" '
        'data-en="Sign in to manage the providers your scheduled tasks connect to." '
        'data-zh="登入以管理你排程任務所連接的服務。">'
        "Sign in to manage the providers your scheduled tasks connect to.</p>\n"
        '<div class="conn-actions">'
        f'<a class="btn btn-primary" href="{safe_url}" '
        'data-en="Sign in" data-zh="登入">Sign in</a>'
        "</div>\n"
        "</main>\n" + _FOOTER + _LANG_JS + "\n</body>\n</html>"
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def make_routes(
    session_factory: async_sessionmaker[AsyncSession],
) -> list[Route]:
    """Return the list of Starlette Route objects for the connections web surface.

    Accepts a session_factory so tests can inject a per-test factory without
    disturbing the module-level engine pool.
    """

    async def _connections_dashboard(request: Request) -> Response:
        user_id = _get_session_user(request)
        if not user_id:
            return RedirectResponse("/connections/login", status_code=302)

        envelope = _make_kms_envelope()
        connected_providers: list[str] = []
        if envelope is not None:
            try:
                async with session_factory() as session:
                    store = ConnectionStore(session, envelope)
                    infos = await store.list(user_id)
                    connected_providers = [info.provider for info in infos]
            except Exception:
                # Render an empty dashboard rather than 500-ing on a transient
                # DB/KMS failure; the user can still hit Connect to retry.
                logger.exception("failed to list connections for user %s", user_id)

        html = _render_dashboard(user_id, connected_providers)
        return HTMLResponse(html)

    async def _connections_login(request: Request) -> Response:
        """Redirect to WorkOS login page, or set trust-only session in dev mode."""
        _wb_enabled = bool(
            settings.workos_client_id and settings.workos_client_secret and settings.workos_issuer
        )
        if _wb_enabled:
            state = secrets.token_urlsafe(24)
            cb = f"{settings.connections_base_url}/connections/auth/callback"
            params = {
                "response_type": "code",
                "client_id": settings.workos_client_id,
                "redirect_uri": cb,
                # `scope=openid` keeps id_token issuance enabled so the
                # callback's preferred signed-JWT path still works; if it
                # ever drops out, we fall back to the (TLS-protected) user.id
                # body field — see _workos_auth_callback below.
                "scope": "openid profile email",
                "state": state,
                # AuthKit User Management flow (issue #203). provider=authkit
                # lets the hosted AuthKit UI handle the login chooser; the
                # returned code is exchanged below at /user_management/authenticate
                # and yields a `user_*` sub matching what the MCP bearer flow
                # (CIMD/DCR) resolves.  Replaces the prior /sso/authorize +
                # provider=GitHubOAuth combination, which issued `prof_*` subs
                # divergent from MCP's `user_*` subs.
                "provider": "authkit",
            }
            qs = urllib.parse.urlencode(params)
            # NB: User Management endpoints are hosted on api.workos.com,
            # NOT on the AuthKit subdomain (workos_issuer).  AuthKit aliases
            # /sso/* for legacy SSO but returns 404 for /user_management/*.
            # See ADR-063 and settings.workos_api_base_url.
            authorize_url = f"{settings.workos_api_base_url}/user_management/authorize?{qs}"
            resp = RedirectResponse(authorize_url, status_code=302)
            resp.set_cookie(_STATE_COOKIE, state, httponly=True, samesite="lax", max_age=600)
            return resp
        else:
            # Trust-only dev mode: set session as MCP_USER_ID / default-user
            user_id = resolve_user_id_stdio()
            resp = RedirectResponse("/connections", status_code=302)
            _set_session(resp, user_id)
            return resp

    async def _workos_auth_callback(request: Request) -> Response:
        """Exchange WorkOS authorization code for a session."""
        code = request.query_params.get("code")
        state = request.query_params.get("state")
        stored_state = request.cookies.get(_STATE_COOKIE)

        if not code:
            return HTMLResponse("Missing code parameter", status_code=400)
        if stored_state and state != stored_state:
            return HTMLResponse("Invalid state parameter", status_code=400)

        # Exchange code via AuthKit User Management /authenticate (issue #203).
        # Hosted on api.workos.com (settings.workos_api_base_url), NOT on the
        # AuthKit subdomain — see ADR-063 host-distinction note.
        # Returns `user.id` in the `user_*` schema — matches MCP bearer sub.
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"{settings.workos_api_base_url}/user_management/authenticate",
                    data={
                        "grant_type": "authorization_code",
                        "code": code,
                        "client_id": settings.workos_client_id,
                        "client_secret": settings.workos_client_secret,
                    },
                )
        except httpx.RequestError as exc:
            return HTMLResponse(f"Auth error: {exc}", status_code=502)

        if not resp.is_success:
            return HTMLResponse(f"Token exchange failed: {resp.text}", status_code=502)

        token_data = resp.json()
        access_token = token_data.get("access_token")
        if not access_token:
            return HTMLResponse("No access_token in response", status_code=502)

        # Extract user_id from the JWT (same validation as Bearer token path)
        try:
            ctx = validate_token(
                access_token,
                issuer=settings.workos_issuer,  # type: ignore[arg-type]
                audience=settings.workos_audience or "",
                jwks_uri=settings.workos_jwks_uri,  # type: ignore[arg-type]
            )
            user_id = ctx.user_id
        except TokenValidationError:
            # access_token didn't carry sub; fall back to id_token.
            # Signature MUST be verified — trusting an unverified payload would
            # let an attacker control the session user identity (issue #160).
            id_token = token_data.get("id_token", "")
            if not id_token or not settings.workos_jwks_uri or not settings.workos_issuer:
                user_id = ""
            else:
                try:
                    user_id = _verify_id_token_sub(
                        id_token,
                        issuer=settings.workos_issuer,  # type: ignore[arg-type]
                        client_id=settings.workos_client_id or "",
                        jwks_uri=settings.workos_jwks_uri,  # type: ignore[arg-type]
                    )
                except TokenValidationError:
                    logger.warning("WorkOS id_token signature verification failed")
                    user_id = ""
        if not user_id:
            # AuthKit User Management /authenticate returns the verified user
            # identity in the top-level `user` object (User Management) rather
            # than as a JWT claim. This branch is reached when both JWT paths
            # above gave up — typical when access_token is opaque.
            #
            # Trust posture: user.id arrives over TLS in the response body of
            # a request we made with our client_secret. Unlike issue #160 (which
            # was about accepting an unverified JWT from the *client*), here the
            # value is sourced from WorkOS directly, so no signature is needed.
            user_obj = token_data.get("user") or {}
            user_id = user_obj.get("id") or ""
        if not user_id:
            return HTMLResponse("Could not determine user identity from token", status_code=502)

        resp = RedirectResponse("/connections", status_code=302)
        _set_session(resp, user_id)
        resp.delete_cookie(_STATE_COOKIE)
        return resp

    async def _connections_logout(request: Request) -> Response:
        resp = RedirectResponse("/connections/login", status_code=302)
        resp.delete_cookie(_SESSION_COOKIE)
        return resp

    async def _github_connect(request: Request) -> Response:
        """Start GitHub OAuth flow."""
        user_id = _get_session_user(request)
        if not user_id:
            return RedirectResponse("/connections/login", status_code=302)

        if not settings.github_client_id:
            return HTMLResponse(
                "GitHub OAuth not configured (GITHUB_CLIENT_ID missing)", status_code=503
            )

        state = secrets.token_urlsafe(24)
        redirect_uri = f"{settings.connections_base_url}/connections/github/callback"
        params = {
            "client_id": settings.github_client_id,
            "redirect_uri": redirect_uri,
            "scope": _GITHUB_OAUTH_SCOPE,
            "state": state,
        }
        github_url = "https://github.com/login/oauth/authorize?" + urllib.parse.urlencode(params)
        resp = RedirectResponse(github_url, status_code=302)
        resp.set_cookie(_STATE_COOKIE, state, httponly=True, samesite="lax", max_age=600)
        return resp

    async def _github_callback(request: Request) -> Response:
        """Handle GitHub OAuth callback — exchange code for token and store it."""
        user_id = _get_session_user(request)
        if not user_id:
            return RedirectResponse("/connections/login", status_code=302)

        code = request.query_params.get("code")
        state = request.query_params.get("state")
        stored_state = request.cookies.get(_STATE_COOKIE)

        if not code:
            return HTMLResponse("Missing code parameter from GitHub", status_code=400)
        if stored_state and state != stored_state:
            return HTMLResponse("Invalid state — possible CSRF", status_code=400)

        envelope = _make_kms_envelope()
        if envelope is None:
            return HTMLResponse(
                "GitHub connection could not be stored (KMS not configured)",
                status_code=503,
            )

        # Exchange code for access token
        redirect_uri = f"{settings.connections_base_url}/connections/github/callback"
        try:
            token_data = await _exchange_github_code(code, redirect_uri)
        except Exception as exc:
            return HTMLResponse(f"GitHub token exchange failed: {exc}", status_code=502)

        # Store in the connection store
        async with session_factory() as session:
            store = ConnectionStore(session, envelope)
            await store.upsert(user_id, "github", token_data)
            await session.commit()

        resp = RedirectResponse("/connections", status_code=302)
        resp.delete_cookie(_STATE_COOKIE)
        return resp

    async def _github_disconnect(request: Request) -> Response:
        """Remove GitHub connection and revoke upstream token."""
        user_id = _get_session_user(request)
        if not user_id:
            return RedirectResponse("/connections/login", status_code=302)

        envelope = _make_kms_envelope()
        if envelope is not None:
            async with session_factory() as session:
                store = ConnectionStore(session, envelope)
                try:
                    token = await store.get_fresh_token(user_id, "github")
                    # Revoke the token upstream if client credentials are configured
                    if settings.github_client_id and settings.github_client_secret:
                        await _revoke_github_token(token)
                except ConnectionMiss:
                    pass
                await store.delete(user_id, "github")
                await session.commit()

        return RedirectResponse("/connections", status_code=302)

    async def _slack_connect(request: Request) -> Response:
        """Start Slack OAuth flow."""
        user_id = _get_session_user(request)
        if not user_id:
            return RedirectResponse("/connections/login", status_code=302)

        if not settings.slack_client_id:
            return HTMLResponse(
                "Slack OAuth not configured (SLACK_CLIENT_ID missing)", status_code=503
            )

        state = secrets.token_urlsafe(24)
        redirect_uri = f"{settings.connections_base_url}/connections/slack/callback"
        params = {
            "client_id": settings.slack_client_id,
            "redirect_uri": redirect_uri,
            "scope": _SLACK_OAUTH_SCOPE,
            "state": state,
        }
        slack_url = "https://slack.com/oauth/v2/authorize?" + urllib.parse.urlencode(params)
        resp = RedirectResponse(slack_url, status_code=302)
        resp.set_cookie(_STATE_COOKIE, state, httponly=True, samesite="lax", max_age=600)
        return resp

    async def _slack_callback(request: Request) -> Response:
        """Handle Slack OAuth callback — exchange code for token and store it."""
        user_id = _get_session_user(request)
        if not user_id:
            return RedirectResponse("/connections/login", status_code=302)

        code = request.query_params.get("code")
        state = request.query_params.get("state")
        stored_state = request.cookies.get(_STATE_COOKIE)

        if not code:
            return HTMLResponse("Missing code parameter from Slack", status_code=400)
        if stored_state and state != stored_state:
            return HTMLResponse("Invalid state — possible CSRF", status_code=400)

        envelope = _make_kms_envelope()
        if envelope is None:
            return HTMLResponse(
                "Slack connection could not be stored (KMS not configured)",
                status_code=503,
            )

        redirect_uri = f"{settings.connections_base_url}/connections/slack/callback"
        try:
            token_data = await _exchange_slack_code(code, redirect_uri)
        except Exception as exc:
            return HTMLResponse(f"Slack token exchange failed: {exc}", status_code=502)

        async with session_factory() as session:
            store = ConnectionStore(session, envelope)
            await store.upsert(user_id, "slack", token_data)
            await session.commit()

        resp = RedirectResponse("/connections", status_code=302)
        resp.delete_cookie(_STATE_COOKIE)
        return resp

    async def _slack_disconnect(request: Request) -> Response:
        """Remove Slack connection and revoke upstream token."""
        user_id = _get_session_user(request)
        if not user_id:
            return RedirectResponse("/connections/login", status_code=302)

        envelope = _make_kms_envelope()
        if envelope is not None:
            async with session_factory() as session:
                store = ConnectionStore(session, envelope)
                try:
                    token = await store.get_fresh_token(user_id, "slack")
                    await _revoke_slack_token(token)
                except ConnectionMiss:
                    pass
                await store.delete(user_id, "slack")
                await session.commit()

        return RedirectResponse("/connections", status_code=302)

    async def _google_connect(request: Request) -> Response:
        """Start Google OAuth flow (Gmail send scope)."""
        user_id = _get_session_user(request)
        if not user_id:
            return RedirectResponse("/connections/login", status_code=302)

        if not settings.google_client_id:
            return HTMLResponse(
                "Google OAuth not configured (GOOGLE_CLIENT_ID missing)", status_code=503
            )

        state = secrets.token_urlsafe(24)
        redirect_uri = f"{settings.connections_base_url}/connections/google/callback"
        params = {
            "client_id": settings.google_client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": _GOOGLE_OAUTH_SCOPE,
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
        google_url = _GOOGLE_AUTH_URL + "?" + urllib.parse.urlencode(params)
        resp = RedirectResponse(google_url, status_code=302)
        resp.set_cookie(_STATE_COOKIE, state, httponly=True, samesite="lax", max_age=600)
        return resp

    async def _google_callback(request: Request) -> Response:
        """Handle Google OAuth callback — exchange code for tokens and store them."""
        user_id = _get_session_user(request)
        if not user_id:
            return RedirectResponse("/connections/login", status_code=302)

        code = request.query_params.get("code")
        state = request.query_params.get("state")
        stored_state = request.cookies.get(_STATE_COOKIE)

        if not code:
            return HTMLResponse("Missing code parameter from Google", status_code=400)
        if stored_state and state != stored_state:
            return HTMLResponse("Invalid state — possible CSRF", status_code=400)

        envelope = _make_kms_envelope()
        if envelope is None:
            return HTMLResponse(
                "Google connection could not be stored (KMS not configured)",
                status_code=503,
            )

        redirect_uri = f"{settings.connections_base_url}/connections/google/callback"
        try:
            token_data = await _exchange_google_code(code, redirect_uri)
        except Exception as exc:
            return HTMLResponse(f"Google token exchange failed: {exc}", status_code=502)

        async with session_factory() as session:
            store = ConnectionStore(session, envelope)
            await store.upsert(user_id, "google", token_data)
            await session.commit()

        resp = RedirectResponse("/connections", status_code=302)
        resp.delete_cookie(_STATE_COOKIE)
        return resp

    async def _google_disconnect(request: Request) -> Response:
        """Remove Google connection and revoke upstream token."""
        user_id = _get_session_user(request)
        if not user_id:
            return RedirectResponse("/connections/login", status_code=302)

        envelope = _make_kms_envelope()
        if envelope is not None:
            async with session_factory() as session:
                store = ConnectionStore(session, envelope)
                try:
                    token = await store.get_fresh_token(user_id, "google")
                    await _revoke_google_token(token)
                except ConnectionMiss:
                    pass
                await store.delete(user_id, "google")
                await session.commit()

        return RedirectResponse("/connections", status_code=302)

    return [
        Route("/connections", endpoint=_connections_dashboard, methods=["GET"]),
        Route("/connections/login", endpoint=_connections_login, methods=["GET"]),
        Route("/connections/logout", endpoint=_connections_logout, methods=["GET"]),
        Route("/connections/auth/callback", endpoint=_workos_auth_callback, methods=["GET"]),
        Route("/connections/github/connect", endpoint=_github_connect, methods=["GET"]),
        Route("/connections/github/callback", endpoint=_github_callback, methods=["GET"]),
        Route("/connections/github/disconnect", endpoint=_github_disconnect, methods=["POST"]),
        Route("/connections/slack/connect", endpoint=_slack_connect, methods=["GET"]),
        Route("/connections/slack/callback", endpoint=_slack_callback, methods=["GET"]),
        Route("/connections/slack/disconnect", endpoint=_slack_disconnect, methods=["POST"]),
        Route("/connections/google/connect", endpoint=_google_connect, methods=["GET"]),
        Route("/connections/google/callback", endpoint=_google_callback, methods=["GET"]),
        Route("/connections/google/disconnect", endpoint=_google_disconnect, methods=["POST"]),
    ]


# ---------------------------------------------------------------------------
# GitHub OAuth helpers
# ---------------------------------------------------------------------------


async def _exchange_github_code(code: str, redirect_uri: str) -> dict[str, Any]:
    """Exchange a GitHub authorization code for an access token.

    Returns the parsed token response dict (contains access_token, scope, etc.).
    Raises on any HTTP or parse error.
    """
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            "https://github.com/login/oauth/access_token",
            headers={"Accept": "application/json"},
            data={
                "client_id": settings.github_client_id,
                "client_secret": settings.github_client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
            },
        )
    resp.raise_for_status()
    data = resp.json()
    if "access_token" not in data:
        raise ValueError(f"GitHub OAuth error: {data.get('error_description', data)}")
    return data


async def _revoke_github_token(token: str) -> None:
    """Attempt to revoke *token* via GitHub's token revocation API.

    Best-effort: errors are silently swallowed (the connection is deleted
    from our store regardless, per ADR-058 revocation semantics).
    """
    if not settings.github_client_id or not settings.github_client_secret:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.delete(
                f"https://api.github.com/applications/{settings.github_client_id}/token",
                auth=(settings.github_client_id, settings.github_client_secret),
                json={"access_token": token},
            )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Slack OAuth helpers
# ---------------------------------------------------------------------------


async def _exchange_slack_code(code: str, redirect_uri: str) -> dict[str, Any]:
    """Exchange a Slack authorization code for an access token.

    Returns the parsed token response dict (contains access_token, scope, etc.).
    Raises on any HTTP or parse error.
    """
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            "https://slack.com/api/oauth.v2.access",
            data={
                "code": code,
                "client_id": settings.slack_client_id,
                "client_secret": settings.slack_client_secret,
                "redirect_uri": redirect_uri,
            },
        )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise ValueError(f"Slack OAuth error: {data.get('error', data)}")
    return data


async def _revoke_slack_token(token: str) -> None:
    """Attempt to revoke *token* via Slack's auth.revoke API.

    Best-effort: errors are silently swallowed (the connection is deleted
    from our store regardless, per ADR-058 revocation semantics).
    """
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                "https://slack.com/api/auth.revoke",
                headers={"Authorization": f"Bearer {token}"},
            )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Google OAuth helpers
# ---------------------------------------------------------------------------


async def _exchange_google_code(code: str, redirect_uri: str) -> dict[str, Any]:
    """Exchange a Google authorization code for access + refresh tokens.

    Computes ``expires_at`` from ``expires_in`` so the connection store can
    check the refresh window without decrypting the blob.
    """
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            _GOOGLE_TOKEN_URL,
            data={
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )
    resp.raise_for_status()
    data = resp.json()
    if "access_token" not in data:
        raise ValueError(f"Google OAuth error: {data.get('error_description', data)}")
    if "expires_in" in data:
        expires_at = datetime.now(UTC) + timedelta(seconds=int(data["expires_in"]))
        data["expires_at"] = expires_at.isoformat()
    return data


async def _revoke_google_token(token: str) -> None:
    """Attempt to revoke *token* via Google's token revocation endpoint.

    Best-effort: errors are silently swallowed (the connection is deleted
    from our store regardless, per ADR-058 revocation semantics).
    """
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(_GOOGLE_REVOKE_URL, params={"token": token})
    except Exception:
        pass
