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
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware

from spark_pulse import sessions
from spark_pulse.config import config

router = APIRouter(prefix="/auth", tags=["auth"])

#: How long an unredeemed OIDC ``state`` stays valid. The round trip is one
#: browser redirect to the provider and back; ten minutes is generous.
_STATE_TTL_SECONDS = 600

#: Issued ``state`` values, mapped to ``(minted_at, nonce)``. Verified and
#: consumed on the callback — without this the callback accepts a code from
#: anyone, which is login CSRF: an attacker can complete the flow in the
#: victim's browser and land them in the attacker's session.
#:
#: The nonce travels with the state because it is the *same* round trip: the
#: state ties the callback to a login we started, and the nonce ties the ID
#: token to it. Keeping them in one entry means neither can be checked against
#: a login the other did not come from.
_pending_states: dict[str, tuple[float, str]] = {}

#: How long a discovery document is reused before it is fetched again. The
#: provider's endpoints change about as often as the provider does, and
#: fetching them on every login turns each one into two round trips.
_DISCOVERY_TTL_SECONDS = 3600

#: ``provider_url`` → ``(fetched_at, document)``.
_discovery_cache: dict[str, tuple[float, dict]] = {}


def _sweep_states(now: float | None = None) -> None:
    now = time.time() if now is None else now
    for state, (minted, _nonce) in list(_pending_states.items()):
        if now - minted > _STATE_TTL_SECONDS:
            _pending_states.pop(state, None)


def _issue_state() -> tuple[str, str]:
    """A fresh ``(state, nonce)`` pair, remembered until the callback."""
    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    _sweep_states()
    _pending_states[state] = (time.time(), nonce)
    return state, nonce


def _consume_state(state: str) -> str | None:
    """The nonce issued with ``state``, or None if we did not issue it.

    Single-use: a state redeemed twice is a replayed callback, and the second
    attempt gets None.
    """
    _sweep_states()
    entry = _pending_states.pop(state, None)
    return None if entry is None else entry[1]


def _session_expired(user: dict, now: float | None = None) -> bool:
    """Whether a stored session has passed the expiry it was created with.

    Kept as a pure predicate even though the store now enforces expiry on
    read: a caller holding a session dict can still ask, and the answer must
    not depend on a round trip.
    """
    expires_at = user.get("expires_at")
    if not expires_at:
        return False
    return (time.time() if now is None else now) >= float(expires_at)


async def _load_session(token: str) -> dict | None:
    """The session behind a cookie, read off the event loop.

    ``run_in_threadpool`` because the store is synchronous — which is §3.3's
    "all access off the event loop", and which is what keeps this correct when
    the URL points at PostgreSQL across a network rather than at a local file.
    """
    return await run_in_threadpool(sessions.get, token)


async def discover(provider_url: str, *, refresh: bool = False) -> dict:
    """The provider's OpenID configuration, cached.

    **Every endpoint comes from here.** The callback used to append
    ``/oauth2/token`` and ``/userinfo`` to the provider URL while ``/login``
    discovered its authorization endpoint properly — so login worked against a
    provider that happened to lay its endpoints out that way and failed
    against every provider that does not. Keycloak, which this module's own
    docstring uses as its example, serves ``/protocol/openid-connect/token``;
    a realm URL configured exactly as documented could not log anybody in.

    Raises :class:`HTTPException` rather than returning a partial document: a
    provider we cannot describe is one we must not start a flow with.
    """
    import httpx

    cached = _discovery_cache.get(provider_url)
    if cached and not refresh and time.time() - cached[0] < _DISCOVERY_TTL_SECONDS:
        return cached[1]

    url = f"{provider_url.rstrip('/')}/.well-known/openid-configuration"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(url)
        if response.status_code != 200:
            raise HTTPException(status_code=502, detail="OIDC provider unreachable")
        document = response.json()
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=502, detail="Failed to discover OIDC provider")

    if not isinstance(document, dict) or not document.get("authorization_endpoint"):
        raise HTTPException(
            status_code=500, detail="OIDC discovery document is unusable"
        )
    _discovery_cache[provider_url] = (time.time(), document)
    return document


async def _verify_id_token(id_token: str, document: dict, nonce: str) -> dict:
    """Verify the ID token's signature and claims, and return them.

    The token was never looked at: the flow trusted the token endpoint's
    response because it had been fetched over TLS with the client secret.
    That is *an* argument, but it leaves the assertion itself unchecked, and
    it leaves the nonce — the only thing tying this token to the login that
    asked for it — unverified, so an ID token captured from one login could be
    replayed into another.

    ``authlib`` has been a declared dependency of this project since the
    beginning and was imported nowhere. This is what it was for.
    """
    import httpx
    from authlib.jose import JsonWebKey, JsonWebToken
    from authlib.jose.errors import JoseError

    jwks_uri = document.get("jwks_uri")
    if not jwks_uri:
        raise HTTPException(
            status_code=502, detail="OIDC provider publishes no JWKS endpoint"
        )
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            jwks_response = await client.get(jwks_uri)
        key_set = JsonWebKey.import_key_set(jwks_response.json())
    except Exception:
        raise HTTPException(
            status_code=502, detail="Could not fetch the OIDC signing keys"
        )

    algorithms = document.get("id_token_signing_alg_values_supported") or ["RS256"]
    # "none" would let anyone mint an ID token; it is removed whatever the
    # provider advertises.
    algorithms = [alg for alg in algorithms if str(alg).lower() != "none"]
    try:
        claims = JsonWebToken(algorithms).decode(
            id_token,
            key_set,
            claims_options={
                "iss": {"essential": True, "values": [document.get("issuer")]},
                "aud": {"essential": True, "values": [config.oidc_client_id]},
            },
        )
        claims.validate(leeway=60)
    except JoseError as exc:
        raise HTTPException(status_code=401, detail=f"Invalid ID token: {exc}")

    if nonce and claims.get("nonce") != nonce:
        raise HTTPException(
            status_code=401, detail="ID token was issued for a different login"
        )
    return dict(claims)


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

#: The path the identity provider sends the browser back to.
CALLBACK_PATH = "/auth/callback"


def callback_url(request: Request) -> str:
    """Where the provider should return the browser to.

    ``config.external_url`` when the operator has stated it, and only
    otherwise the request's own URL — whose host is the ``Host`` header, which
    the *client* chooses. An attacker who can set that header picks the
    ``redirect_uri`` we hand the provider, and a provider with a loose
    redirect allowlist then sends the authorization code wherever they asked.

    The same function serves ``/auth/login`` and ``/auth/callback`` because
    the two values must be byte-identical: the token endpoint compares the
    ``redirect_uri`` of the exchange against the one the code was issued for,
    and computes them separately is how they drift apart.
    """
    external = config.external_url
    if external:
        return f"{external}{CALLBACK_PATH}"
    return str(request.url.replace(path=CALLBACK_PATH, query=""))


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

        # The store drops an expired row as it reads it, so "expired" and
        # "never existed" arrive here as the same answer — which is all this
        # needs to know, and is one round trip rather than two.
        user = await _load_session(token)
        if not user:
            return JSONResponse(
                status_code=401, content={"detail": "Not authenticated"}
            )

        # Attach user to request state
        request.state.user = user
        return await call_next(request)


# ── Auth routes ──────────────────────────────────────────────────────────────


@router.get("/login")
async def login(request: Request):
    """Redirect to OIDC provider login page."""
    if not _oidc_configured():
        return {"message": "Authentication is not configured"}

    document = await discover(config.oidc_provider_url)
    auth_url = document.get("authorization_endpoint", "")
    if not auth_url:
        raise HTTPException(
            status_code=500, detail="Missing authorization_endpoint in OIDC config"
        )

    state, nonce = _issue_state()
    redirect_uri = callback_url(request)

    params = {
        "response_type": "code",
        "client_id": config.oidc_client_id,
        "redirect_uri": redirect_uri,
        "scope": "openid profile email",
        "state": state,
        # Bound to the state above and checked against the ID token, so a
        # token minted for one login cannot be replayed into another.
        "nonce": nonce,
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
    nonce = _consume_state(state)
    if nonce is None:
        raise HTTPException(
            status_code=400, detail="Invalid or expired authentication state"
        )

    document = await discover(config.oidc_provider_url)
    token_endpoint = document.get("token_endpoint")
    if not token_endpoint:
        raise HTTPException(
            status_code=500, detail="Missing token_endpoint in OIDC config"
        )

    try:
        import httpx

        # The same value /auth/login sent, computed the same way: the token
        # endpoint compares it against the one the code was issued for.
        redirect_uri = callback_url(request)

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                token_endpoint,
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

            # The assertion itself, checked. We asked for the ``openid``
            # scope, so a provider that returns no ID token has not answered
            # the question we asked and is refused rather than trusted.
            id_token = token_data.get("id_token", "")
            if not id_token:
                raise HTTPException(
                    status_code=401, detail="OIDC provider returned no ID token"
                )
            claims = await _verify_id_token(id_token, document, nonce)

            # Get user info from the endpoint the provider advertises. The
            # verified ID token is the fallback *and* the floor: it already
            # carries a trustworthy subject, so a userinfo call that fails
            # degrades to a smaller session rather than an anonymous one.
            user_info: dict = {"sub": claims.get("sub", "")}
            userinfo_endpoint = document.get("userinfo_endpoint")
            if userinfo_endpoint:
                user_resp = await client.get(
                    userinfo_endpoint,
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                if user_resp.status_code == 200:
                    published = user_resp.json()
                    if isinstance(published, dict):
                        # The subject stays the verified one: userinfo is
                        # fetched with a bearer token and must not be able to
                        # rename the principal the ID token established.
                        user_info = {**published, "sub": claims.get("sub", "")}

            # Store token, dropping anything that has already expired so the
            # store stays bounded by the number of live sessions.
            await run_in_threadpool(sessions.sweep)
            token_key = await run_in_threadpool(
                lambda: sessions.create(
                    access_token=access_token,
                    refresh_token=refresh_token,
                    expires_at=datetime.now(timezone.utc).timestamp() + expires_in,
                    user=user_info,
                    created_at=datetime.now(timezone.utc).isoformat(),
                )
            )

            # Set session cookie and redirect to home
            response = RedirectResponse(url="/", status_code=302)
            response.set_cookie(
                key="token",
                value=token_key,
                httponly=True,
                samesite="lax",
                path="/",
                max_age=expires_in,
                # Set whenever the flow itself ran over TLS. Hardcoding it on
                # would break the ordinary http://localhost install; hardcoding
                # it off — which is what it was — sends the session cookie in
                # clear the moment anyone puts this behind HTTPS.
                secure=request.url.scheme == "https",
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
        await run_in_threadpool(sessions.remove, token)
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

    user_data = await _load_session(token)
    if not user_data:
        raise HTTPException(status_code=401, detail="Not authenticated")

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
