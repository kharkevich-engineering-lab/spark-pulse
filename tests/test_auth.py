import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from spark_pulse import auth


class DummyRequest:
    def __init__(self, user=None):
        self.state = SimpleNamespace(user=user)


# ── _oidc_configured tests ──────────────────────────────────────────────────


def test_oidc_configured_true(monkeypatch):
    monkeypatch.setenv("SPARK_PULSE_AUTH_ENABLED", "true")
    monkeypatch.setitem(
        auth.config._data, "oidc_provider_url", "https://issuer.example"
    )
    monkeypatch.setitem(auth.config._data, "oidc_client_id", "client-id")
    monkeypatch.setitem(auth.config._data, "oidc_client_secret", "secret")

    assert auth._oidc_configured() is True


def test_oidc_configured_false_when_disabled(monkeypatch):
    monkeypatch.setenv("SPARK_PULSE_AUTH_ENABLED", "false")
    monkeypatch.setitem(
        auth.config._data, "oidc_provider_url", "https://issuer.example"
    )
    monkeypatch.setitem(auth.config._data, "oidc_client_id", "client-id")
    monkeypatch.setitem(auth.config._data, "oidc_client_secret", "secret")

    assert auth._oidc_configured() is False


def test_oidc_configured_false_when_missing_provider_url(monkeypatch):
    monkeypatch.setenv("SPARK_PULSE_AUTH_ENABLED", "true")
    monkeypatch.setitem(auth.config._data, "oidc_provider_url", "")
    monkeypatch.setitem(auth.config._data, "oidc_client_id", "client-id")
    monkeypatch.setitem(auth.config._data, "oidc_client_secret", "secret")

    assert auth._oidc_configured() is False


def test_oidc_configured_false_when_missing_client_id(monkeypatch):
    monkeypatch.setenv("SPARK_PULSE_AUTH_ENABLED", "true")
    monkeypatch.setitem(
        auth.config._data, "oidc_provider_url", "https://issuer.example"
    )
    monkeypatch.setitem(auth.config._data, "oidc_client_id", "")
    monkeypatch.setitem(auth.config._data, "oidc_client_secret", "secret")

    assert auth._oidc_configured() is False


def test_oidc_configured_false_when_missing_client_secret(tmp_path, monkeypatch):
    """OIDC should not be configured when client secret is empty (no fallback to secrets.json)."""
    from spark_pulse import config as config_module

    monkeypatch.setenv("SPARK_PULSE_AUTH_ENABLED", "true")
    monkeypatch.setitem(
        auth.config._data, "oidc_provider_url", "https://issuer.example"
    )
    monkeypatch.setitem(auth.config._data, "oidc_client_id", "client-id")
    monkeypatch.setitem(auth.config._data, "oidc_client_secret", "")
    monkeypatch.setattr(
        config_module, "_SECRETS_PATH", tmp_path / "missing_secrets.json"
    )

    assert auth._oidc_configured() is False


# ── get_current_user tests ──────────────────────────────────────────────────


def test_get_current_user_returns_user():
    user = {"name": "Alice"}

    out = auth.get_current_user(DummyRequest(user=user))

    assert out == user


def test_get_current_user_raises_when_missing_user():
    with pytest.raises(HTTPException) as exc:
        auth.get_current_user(DummyRequest())

    assert exc.value.status_code == 401


# ── AuthMiddleware tests ────────────────────────────────────────────────────


class TestAuthMiddleware:
    """Tests for the AuthMiddleware class."""

    def _make_request(self, path="/api/recipes", cookie_token=None):
        """Create a mock ASGI scope for testing."""
        from unittest.mock import MagicMock

        request = MagicMock()
        request.url.path = path
        request.url.__str__ = MagicMock(return_value=f"http://localhost{path}")
        request.headers = {}
        request.cookies = {"token": cookie_token} if cookie_token else {}
        return request

    def test_dispatch_allows_when_auth_disabled(self, monkeypatch):
        """When auth is disabled, all requests should pass through."""
        from unittest.mock import MagicMock

        monkeypatch.setenv("SPARK_PULSE_AUTH_ENABLED", "false")
        mw = auth.AuthMiddleware(None)
        request = self._make_request("/api/recipes")
        call_next_called = []

        async def call_next(_):
            call_next_called.append(True)
            return MagicMock()

        asyncio.run(mw.dispatch(request, call_next))
        assert len(call_next_called) == 1

    def test_dispatch_allows_public_paths(self, monkeypatch):
        """Public paths should be allowed without auth."""
        from unittest.mock import MagicMock

        monkeypatch.setenv("SPARK_PULSE_AUTH_ENABLED", "true")
        monkeypatch.setitem(
            auth.config._data, "oidc_provider_url", "https://issuer.example"
        )
        monkeypatch.setitem(auth.config._data, "oidc_client_id", "client-id")
        monkeypatch.setitem(auth.config._data, "oidc_client_secret", "secret")
        mw = auth.AuthMiddleware(None)
        for path in [
            "/health",
            "/auth/login",
            "/auth/callback",
            "/auth/logout",
            "/auth/me",
            "/api/config",
        ]:
            request = self._make_request(path)
            call_next_called = []

            async def call_next(_):
                call_next_called.append(True)
                return MagicMock()

            asyncio.run(mw.dispatch(request, call_next))
            assert len(call_next_called) == 1, f"Path {path} should be public"

    def test_dispatch_returns_401_without_cookie(self, monkeypatch):
        """Requests without session cookie should return 401."""
        from unittest.mock import MagicMock
        from starlette.responses import JSONResponse

        monkeypatch.setenv("SPARK_PULSE_AUTH_ENABLED", "true")
        monkeypatch.setitem(
            auth.config._data, "oidc_provider_url", "https://issuer.example"
        )
        monkeypatch.setitem(auth.config._data, "oidc_client_id", "client-id")
        monkeypatch.setitem(auth.config._data, "oidc_client_secret", "secret")
        mw = auth.AuthMiddleware(None)
        request = self._make_request("/api/recipes")
        call_next_called = []

        async def call_next(_):
            call_next_called.append(True)
            return MagicMock()

        result = asyncio.run(mw.dispatch(request, call_next))
        assert len(call_next_called) == 0
        assert isinstance(result, JSONResponse)
        assert result.status_code == 401

    def test_returns_401_with_invalid_cookie(self, monkeypatch):
        """Requests with an unknown cookie should return 401."""
        from unittest.mock import MagicMock
        from starlette.responses import JSONResponse

        monkeypatch.setenv("SPARK_PULSE_AUTH_ENABLED", "true")
        monkeypatch.setitem(
            auth.config._data, "oidc_provider_url", "https://issuer.example"
        )
        monkeypatch.setitem(auth.config._data, "oidc_client_id", "client-id")
        monkeypatch.setitem(auth.config._data, "oidc_client_secret", "secret")
        mw = auth.AuthMiddleware(None)
        request = self._make_request("/api/recipes", cookie_token="unknown-token")
        call_next_called = []

        async def call_next(_):
            call_next_called.append(True)
            return MagicMock()

        result = asyncio.run(mw.dispatch(request, call_next))
        assert len(call_next_called) == 0
        assert isinstance(result, JSONResponse)
        assert result.status_code == 401

    def test_attaches_user_with_valid_cookie(self, monkeypatch):
        """Requests with a valid cookie should attach user to request state."""
        from unittest.mock import MagicMock

        monkeypatch.setenv("SPARK_PULSE_AUTH_ENABLED", "true")
        monkeypatch.setitem(
            auth.config._data, "oidc_provider_url", "https://issuer.example"
        )
        monkeypatch.setitem(auth.config._data, "oidc_client_id", "client-id")
        monkeypatch.setitem(auth.config._data, "oidc_client_secret", "secret")
        mw = auth.AuthMiddleware(None)
        valid_token = "valid-token-123"
        auth._active_tokens[valid_token] = {"user": {"name": "Alice"}}

        request = self._make_request("/api/recipes", cookie_token=valid_token)
        call_next_called = []

        async def call_next(_):
            call_next_called.append(True)
            return MagicMock()

        _result = asyncio.run(mw.dispatch(request, call_next))
        assert len(call_next_called) == 1
        # Middleware attaches the full token data (including "user" key)
        assert request.state.user == {"user": {"name": "Alice"}}


# ── Auth routes tests ───────────────────────────────────────────────────────


class TestAuthRoutes:
    """Tests for auth API routes."""

    def test_logout_without_bearer_token(self, monkeypatch):
        """Logout without a Bearer token should return success (no-op)."""
        from fastapi.testclient import TestClient
        from spark_pulse.app import create_app

        monkeypatch.setenv("SPARK_PULSE_AUTH_ENABLED", "true")
        monkeypatch.setitem(
            auth.config._data, "oidc_provider_url", "https://issuer.example"
        )
        monkeypatch.setitem(auth.config._data, "oidc_client_id", "client-id")
        monkeypatch.setitem(auth.config._data, "oidc_client_secret", "secret")

        app = create_app()
        client = TestClient(app)

        resp = client.post("/auth/logout")
        assert resp.status_code == 200

    def test_get_me_without_token(self, monkeypatch):
        """GET /auth/me without a token should return 401."""
        from fastapi.testclient import TestClient
        from spark_pulse.app import create_app

        monkeypatch.setenv("SPARK_PULSE_AUTH_ENABLED", "true")
        monkeypatch.setitem(
            auth.config._data, "oidc_provider_url", "https://issuer.example"
        )
        monkeypatch.setitem(auth.config._data, "oidc_client_id", "client-id")
        monkeypatch.setitem(auth.config._data, "oidc_client_secret", "secret")

        app = create_app()
        client = TestClient(app)

        resp = client.get("/auth/me")
        assert resp.status_code == 401

    def test_get_me_with_invalid_token(self, monkeypatch):
        """GET /auth/me with an invalid token should return 401."""
        from fastapi.testclient import TestClient
        from spark_pulse.app import create_app

        monkeypatch.setenv("SPARK_PULSE_AUTH_ENABLED", "true")
        monkeypatch.setitem(
            auth.config._data, "oidc_provider_url", "https://issuer.example"
        )
        monkeypatch.setitem(auth.config._data, "oidc_client_id", "client-id")
        monkeypatch.setitem(auth.config._data, "oidc_client_secret", "secret")

        app = create_app()
        client = TestClient(app)

        resp = client.get("/auth/me", headers={"Authorization": "Bearer invalid"})
        assert resp.status_code == 401

    def test_get_me_with_valid_token(self, monkeypatch):
        """GET /auth/me with a valid token should return user info."""
        from fastapi.testclient import TestClient
        from spark_pulse.app import create_app

        monkeypatch.setenv("SPARK_PULSE_AUTH_ENABLED", "true")
        monkeypatch.setitem(
            auth.config._data, "oidc_provider_url", "https://issuer.example"
        )
        monkeypatch.setitem(auth.config._data, "oidc_client_id", "client-id")
        monkeypatch.setitem(auth.config._data, "oidc_client_secret", "secret")

        app = create_app()
        client = TestClient(app)

        # Insert a valid token
        valid_token = "test-token-456"
        auth._active_tokens[valid_token] = {
            "user": {"name": "Bob", "email": "bob@example.com"},
        }

        resp = client.get(
            "/auth/me",
            cookies={"token": valid_token},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["authenticated"] is True
        assert data["user"]["name"] == "Bob"
        assert data["user"]["email"] == "bob@example.com"


# ── The OIDC round trip's own integrity ──────────────────────────────────────


class TestOidcState:
    """``state`` exists to tie a callback to a login this server started.

    It was minted at ``/auth/login``, embedded in the redirect, and then never
    looked at again — so ``/auth/callback`` accepted an authorization code
    from anybody. That is login CSRF: an attacker completes the flow in the
    victim's browser and the victim ends up working inside the attacker's
    account, on the attacker's data, believing it is their own.
    """

    def test_a_state_is_single_use(self):
        state, nonce = auth._issue_state()

        assert auth._consume_state(state) == nonce
        assert auth._consume_state(state) is None, "a replayed state is refused"

    def test_a_state_we_never_issued_is_refused(self):
        assert auth._consume_state("not-one-of-ours") is None

    def test_a_state_expires(self, monkeypatch):
        state, _nonce = auth._issue_state()
        minted, nonce = auth._pending_states[state]
        auth._pending_states[state] = (minted - auth._STATE_TTL_SECONDS - 1, nonce)

        assert auth._consume_state(state) is None

    def test_each_login_gets_its_own_nonce(self):
        """The nonce ties the ID token to *this* login, so it cannot be shared."""
        _first, first_nonce = auth._issue_state()
        _second, second_nonce = auth._issue_state()

        assert first_nonce != second_nonce

    def test_the_callback_refuses_an_unknown_state(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient

        from spark_pulse.app import create_app

        monkeypatch.setenv("SPARK_PULSE_AUTH_ENABLED", "true")
        monkeypatch.setitem(
            auth.config._data, "oidc_provider_url", "https://issuer.example"
        )
        monkeypatch.setitem(auth.config._data, "oidc_client_id", "client-id")
        monkeypatch.setitem(auth.config._data, "oidc_client_secret", "secret")

        with TestClient(create_app()) as client:
            resp = client.get("/auth/callback?code=stolen&state=forged")

        assert resp.status_code == 400
        assert "state" in resp.json()["detail"].lower()


class TestSessionExpiry:
    """``expires_at`` was recorded at login and then never read.

    A session therefore outlived the provider's own token for as long as the
    process ran, and ``_active_tokens`` grew for every login that ever
    happened. The cookie's ``max-age`` governs only the browser; a client that
    keeps sending an expired cookie was honoured indefinitely.
    """

    def test_an_expired_session_is_refused(self):
        assert auth._session_expired({"expires_at": 1.0}) is True

    def test_a_live_session_is_not(self):
        import time

        assert auth._session_expired({"expires_at": time.time() + 3600}) is False

    def test_a_session_with_no_recorded_expiry_is_kept(self):
        """Sessions written before this field was consulted must not all die."""
        assert auth._session_expired({}) is False

    def test_sweeping_drops_only_the_expired(self):
        import time

        auth._active_tokens.clear()
        auth._active_tokens["old"] = {"expires_at": 1.0}
        auth._active_tokens["new"] = {"expires_at": time.time() + 3600}

        auth._sweep_sessions()

        assert "old" not in auth._active_tokens
        assert "new" in auth._active_tokens
        auth._active_tokens.clear()

    def test_get_me_refuses_an_expired_session(self, monkeypatch):
        from fastapi.testclient import TestClient

        from spark_pulse.app import create_app

        monkeypatch.setenv("SPARK_PULSE_AUTH_ENABLED", "true")
        monkeypatch.setitem(
            auth.config._data, "oidc_provider_url", "https://issuer.example"
        )
        monkeypatch.setitem(auth.config._data, "oidc_client_id", "client-id")
        monkeypatch.setitem(auth.config._data, "oidc_client_secret", "secret")

        auth._active_tokens["stale"] = {"user": {"name": "Bob"}, "expires_at": 1.0}
        with TestClient(create_app()) as client:
            resp = client.get("/auth/me", cookies={"token": "stale"})

        assert resp.status_code == 401
        assert "stale" not in auth._active_tokens, "an expired session is dropped"
