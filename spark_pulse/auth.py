"""OIDC authentication middleware and routes.

Configured via config.yaml:
  auth_enabled: true/false
  oidc_provider_url: https://keycloak.example.com/realms/myrealm
  oidc_client_id: spark-manager
  oidc_client_secret: ...

When auth is disabled, all requests are allowed (no auth required).
When enabled, only /health, /auth/*, and static files are public.
"""

from __future__ import annotations

import os
import secrets
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware

from spark_pulse.config import config

router = APIRouter(prefix="/auth", tags=["auth"])

# In-memory token store (replace with Redis in production)
_active_tokens: dict[str, dict] = {}

#: How long an unredeemed OIDC ``state`` stays valid. The round trip is one
#: browser redirect to the provider and back; ten minutes is generous.
_STATE_TTL_SECONDS = 600

#: Issued ``state`` values, mapped to when they were minted. Verified and
#: consumed on the callback — without this the callback accepts a code from
#: anyone, which is login CSRF: an attacker can complete the flow in the
#: victim's browser and land them in the attacker's session.
_pending_states: dict[str, float] = {}


def _sweep_states(now: float | None = None) -> None:
    now = time.time() if now is None else now
    for state, minted in list(_pending_states.items()):
        if now - minted > _STATE_TTL_SECONDS:
            _pending_states.pop(state, None)


def _issue_state() -> str:
    state = secrets.token_urlsafe(24)
    _sweep_states()
    _pending_states[state] = time.time()
    return state


def _consume_state(state: str) -> bool:
    """Whether ``state`` was one we issued, and has not been used yet."""
    _sweep_states()
    return _pending_states.pop(state, None) is not None


def _session_expired(user: dict, now: float | None = None) -> bool:
    """Whether a stored session has passed the expiry it was created with.

    ``expires_at`` was recorded at login and then never read, so a session
    outlived the provider's own token for as long as the process ran and the
    store grew without bound. Both are fixed by consulting it.
    """
    expires_at = user.get("expires_at")
    if not expires_at:
        return False
    return (time.time() if now is None else now) >= float(expires_at)


def _sweep_sessions() -> None:
    for key, user in list(_active_tokens.items()):
        if _session_expired(user):
            _active_tokens.pop(key, None)


def _oidc_configured() -> bool:
    """Check if OIDC is configured and enabled."""
    return bool(
        os.environ.get("SPARK_PULSE_AUTH_ENABLED", str(config.auth_enabled)) == "true"
        and config.oidc_provider_url
        and config.oidc_client_id
        and config.oidc_client_secret
    )


#: Methods that cannot change anything, and so need no cross-origin defence.
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


def _same_origin(request: Request, origin: str) -> bool:
    """Whether ``origin`` is this server's own, as the request describes it."""
    url = request.url
    return origin == f"{url.scheme}://{url.netloc}"


class CsrfMiddleware(BaseHTTPMiddleware):
    """Refuse state-changing requests that a *different site* originated.

    This is the protection the frontend's ``X-CSRF-Token`` header was always
    supposed to have: the header was read from a ``<meta name="csrf-token">``
    tag that nothing ever emitted, so it was never sent, and nothing on this
    side ever checked it. Both halves were decoration.

    The rule here is the one that does not need a token to be plumbed through
    every client:

    * A request with **no ``Origin`` header** is not from a browser's
      cross-site machinery — ``curl``, the MCP client, the test client, a
      server-to-server call — and is allowed. A browser attaches ``Origin`` to
      every cross-origin request and to every unsafe same-origin one, so this
      exempts exactly the callers that cannot be victims of CSRF.
    * A request **with** an ``Origin`` must carry one this server serves the UI
      on. Anything else is another site driving the operator's browser.

    Combined with a CORS policy that no longer reflects arbitrary origins, a
    page on ``evil.example`` can neither read an answer from this API nor
    provoke a write it cannot read.
    """

    async def dispatch(self, request: Request, call_next):
        if request.method in _SAFE_METHODS:
            return await call_next(request)
        origin = request.headers.get("origin")
        if not origin:
            return await call_next(request)
        if _same_origin(request, origin) or origin.rstrip("/") in set(
            config.cors_allowed_origins
        ):
            return await call_next(request)
        return JSONResponse(
            status_code=403,
            content={"detail": f"Cross-origin request from {origin} refused"},
        )


class AuthMiddleware(BaseHTTPMiddleware):
    """Protect all routes except public ones when auth is enabled."""

    PUBLIC_PATHS = {
        "/",
        "/login",
        "/health",
        "/auth/login",
        "/auth/callback",
        "/auth/logout",
        "/auth/me",
        "/api/config",
    }

    async def dispatch(self, request: Request, call_next):
        if not _oidc_configured():
            # Auth disabled — allow everything
            return await call_next(request)

        # Allow public paths
        path = request.url.path
        if (
            path in self.PUBLIC_PATHS
            or path.startswith("/auth/")
            or path.startswith("/assets/")
            or path.startswith("/static/")
        ):
            return await call_next(request)

        # Require session cookie
        token = request.cookies.get("token")
        if not token:
            return JSONResponse(
                status_code=401, content={"detail": "Not authenticated"}
            )

        user = _active_tokens.get(token)
        if not user:
            return JSONResponse(status_code=401, content={"detail": "Invalid token"})
        if _session_expired(user):
            # The provider's own token has expired, so ours has too. Dropped
            # here rather than left to accumulate: the cookie's max-age only
            # governs the browser, and a client that keeps sending an expired
            # cookie was otherwise honoured for the life of the process.
            _active_tokens.pop(token, None)
            return JSONResponse(status_code=401, content={"detail": "Session expired"})

        # Attach user to request state
        request.state.user = user
        return await call_next(request)


# ── Auth routes ──────────────────────────────────────────────────────────────


@router.get("/login")
async def login(request: Request):
    """Redirect to OIDC provider login page."""
    if not _oidc_configured():
        return {"message": "Authentication is not configured"}

    # Build auth URL
    provider_url = config.oidc_provider_url
    # Discover well-known config
    try:
        import httpx

        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{provider_url}/.well-known/openid-configuration")
            if resp.status_code == 200:
                config_data = resp.json()
                auth_url = config_data.get("authorization_endpoint", "")
            else:
                raise HTTPException(status_code=502, detail="OIDC provider unreachable")
    except Exception:
        raise HTTPException(status_code=502, detail="Failed to discover OIDC provider")

    if not auth_url:
        raise HTTPException(
            status_code=500, detail="Missing authorization_endpoint in OIDC config"
        )

    state = _issue_state()
    redirect_uri = str(request.url.replace(path="/auth/callback", query=""))

    params = {
        "response_type": "code",
        "client_id": config.oidc_client_id,
        "redirect_uri": redirect_uri,
        "scope": "openid profile email",
        "state": state,
    }
    # urlencode, not a hand-rolled join: ``redirect_uri`` carries "://" and
    # "/", and a client id may carry anything at all. Joining raw values
    # produced a URL the provider had to guess at.
    return RedirectResponse(f"{auth_url}?{urlencode(params)}")


@router.get("/callback")
async def callback(request: Request, code: str, state: str):
    """Exchange authorization code for tokens."""
    if not _oidc_configured():
        return {"message": "Authentication is not configured"}

    # The state we issued at /auth/login, and only that one, once. Without
    # this the callback accepts an authorization code from anybody — which is
    # login CSRF: an attacker completes the flow in the victim's browser and
    # the victim ends up working inside the attacker's account.
    if not _consume_state(state):
        raise HTTPException(
            status_code=400, detail="Invalid or expired authentication state"
        )

    try:
        import httpx

        provider_url = config.oidc_provider_url
        redirect_uri = str(request.url.replace(query=""))

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{provider_url}/oauth2/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "client_id": config.oidc_client_id,
                    "client_secret": config.oidc_client_secret,
                },
            )
            if resp.status_code != 200:
                raise HTTPException(status_code=401, detail="Token exchange failed")

            token_data = resp.json()
            access_token = token_data.get("access_token", "")
            refresh_token = token_data.get("refresh_token", "")
            expires_in = token_data.get("expires_in", 3600)

            # Get user info
            user_resp = await client.get(
                f"{provider_url}/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            user_info = user_resp.json() if user_resp.status_code == 200 else {}

            # Store token, dropping anything that has already expired so the
            # store stays bounded by the number of live sessions.
            _sweep_sessions()
            token_key = secrets.token_urlsafe(32)
            _active_tokens[token_key] = {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "expires_at": datetime.now(timezone.utc).timestamp() + expires_in,
                "user": user_info,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }

            # Set session cookie and redirect to home
            response = RedirectResponse(url="/", status_code=302)
            response.set_cookie(
                key="token",
                value=token_key,
                httponly=True,
                samesite="lax",
                path="/",
                max_age=expires_in,
            )
            return response

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/logout")
async def logout(request: Request):
    """Invalidate current token and clear cookie."""
    token = request.cookies.get("token")
    if token:
        _active_tokens.pop(token, None)
    response = {"message": "Logged out"}
    resp = JSONResponse(content=response)
    resp.delete_cookie(key="token", path="/")
    return resp


@router.get("/me")
async def get_me(request: Request):
    """Get current user info."""
    token = request.cookies.get("token")
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    user_data = _active_tokens.get(token)
    if not user_data:
        raise HTTPException(status_code=401, detail="Invalid token")
    if _session_expired(user_data):
        _active_tokens.pop(token, None)
        raise HTTPException(status_code=401, detail="Session expired")

    return {
        "authenticated": True,
        "user": user_data.get("user", {}),
        "provider": config.oidc_provider_url,
    }


def get_current_user(request: Request) -> dict:
    """Get current user from request state (for protected endpoints)."""
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user
