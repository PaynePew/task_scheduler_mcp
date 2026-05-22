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

_HTML_STYLE = """
body { font-family: system-ui, sans-serif; max-width: 700px; margin: 3rem auto; padding: 0 1rem; }
table { border-collapse: collapse; width: 100%; }
th, td { border: 1px solid #ddd; padding: .5rem 1rem; text-align: left; }
th { background: #f4f4f4; }
.btn { display: inline-block; padding: .4rem .9rem; border-radius: 4px;
       text-decoration: none; font-size: .9rem; cursor: pointer; border: none; }
.btn-connect { background: #2ea44f; color: #fff; }
.btn-disconnect { background: #d73a49; color: #fff; }
.status-ok { color: #2ea44f; }
.status-no { color: #888; }
"""


def _render_dashboard(user_id: str, connections: list[str]) -> str:
    """Return a simple HTML string for the connections dashboard."""
    rows = []
    providers = [("GitHub", "github"), ("Slack", "slack"), ("Google", "google")]
    for display_name, provider_slug in providers:
        connected = provider_slug in connections
        if connected:
            status = '<span class="status-ok">Connected</span>'
            disc_url = f"/connections/{provider_slug}/disconnect"
            action = (
                f'<form method="post" action="{disc_url}" style="display:inline">'
                f'<button type="submit" class="btn btn-disconnect">Disconnect</button></form>'
            )
        else:
            status = '<span class="status-no">Not connected</span>'
            conn_url = f"/connections/{provider_slug}/connect"
            action = f'<a href="{conn_url}" class="btn btn-connect">Connect</a>'
        rows.append(f"<tr><td>{display_name}</td><td>{status}</td><td>{action}</td></tr>")

    rows_html = "\n".join(rows)
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>Connections</title><style>{_HTML_STYLE}</style></head>
<body>
  <h1>My Connections</h1>
  <p>Signed in as <code>{user_id}</code> &nbsp;
     <a href="/connections/logout">Sign out</a></p>
  <table>
    <thead><tr><th>Provider</th><th>Status</th><th>Action</th></tr></thead>
    <tbody>{rows_html}</tbody>
  </table>
</body>
</html>"""


def _render_login_page(redirect_url: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>Sign in</title><style>{_HTML_STYLE}</style></head>
<body>
  <h1>Sign in required</h1>
  <p>You need to sign in to manage your connections.</p>
  <a href="{redirect_url}" class="btn btn-connect">Sign in</a>
</body>
</html>"""


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
                "scope": "openid profile email",
                "state": state,
                # WorkOS /sso/authorize requires a connection selector;
                # "authkit" sends the user to the AuthKit hosted UI where
                # they choose between configured social providers / email.
                "provider": "authkit",
            }
            qs = urllib.parse.urlencode(params)
            authorize_url = f"{settings.workos_issuer}/sso/authorize?{qs}"
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

        # Exchange code for token
        redirect_uri = f"{settings.connections_base_url}/connections/auth/callback"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"{settings.workos_issuer}/sso/token",
                    data={
                        "grant_type": "authorization_code",
                        "code": code,
                        "redirect_uri": redirect_uri,
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
