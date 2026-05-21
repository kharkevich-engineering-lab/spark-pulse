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
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware

from spark_pulse.config import config

router = APIRouter(prefix="/auth", tags=["auth"])

# In-memory token store (replace with Redis in production)
_active_tokens: dict[str, dict] = {}


def _oidc_configured() -> bool:
    """Check if OIDC is configured and enabled."""
    return bool(
        os.environ.get("SPARK_PULSE_AUTH_ENABLED", str(config.auth_enabled)) == "true"
        and config.oidc_provider_url
        and config.oidc_client_id
        and config.oidc_client_secret
    )


class AuthMiddleware(BaseHTTPMiddleware):
    """Protect all routes except public ones when auth is enabled."""

    PUBLIC_PATHS = {"/health", "/auth/login", "/auth/callback", "/auth/logout", "/auth/me"}

    async def dispatch(self, request: Request, call_next):
        if not _oidc_configured():
            # Auth disabled — allow everything
            return await call_next(request)

        # Allow public paths
        path = request.url.path
        if path in self.PUBLIC_PATHS or path.startswith("/auth/"):
            return await call_next(request)

        # Require bearer token
        auth_header = request.headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            return RedirectResponse(url="/auth/login")

        token = auth_header[7:]
        user = _active_tokens.get(token)
        if not user:
            return RedirectResponse(url="/auth/login")

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
                token_url = config_data.get("token_endpoint", "")
            else:
                raise HTTPException(status_code=502, detail="OIDC provider unreachable")
    except Exception:
        raise HTTPException(status_code=502, detail="Failed to discover OIDC provider")

    if not auth_url:
        raise HTTPException(status_code=500, detail="Missing authorization_endpoint in OIDC config")

    state = os.urandom(16).hex()
    redirect_uri = str(request.url.replace(path="/auth/callback", query=""))

    params = {
        "response_type": "code",
        "client_id": config.oidc_client_id,
        "redirect_uri": redirect_uri,
        "scope": "openid profile email",
        "state": state,
    }
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return RedirectResponse(f"{auth_url}?{query}")


@router.get("/callback")
async def callback(request: Request, code: str, state: str):
    """Exchange authorization code for tokens."""
    if not _oidc_configured():
        return {"message": "Authentication is not configured"}

    try:
        import httpx
        provider_url = config.oidc_provider_url
        redirect_uri = str(request.url.replace(query=""))

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{provider_url}/token",
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

            # Store token
            token_key = os.urandom(16).hex()
            _active_tokens[token_key] = {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "expires_at": datetime.now(timezone.utc).timestamp() + expires_in,
                "user": user_info,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }

            # Redirect to app with token
            return RedirectResponse(f"/?token={token_key}")

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/logout")
async def logout(request: Request):
    """Invalidate current token."""
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        _active_tokens.pop(token, None)
    return {"message": "Logged out"}


@router.get("/me")
async def get_me(request: Request):
    """Get current user info."""
    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")

    token = auth_header[7:]
    user_data = _active_tokens.get(token)
    if not user_data:
        raise HTTPException(status_code=401, detail="Invalid token")

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
