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


def test_oidc_configured_false_when_missing_client_secret(monkeypatch):
    monkeypatch.setenv("SPARK_PULSE_AUTH_ENABLED", "true")
    monkeypatch.setitem(
        auth.config._data, "oidc_provider_url", "https://issuer.example"
    )
    monkeypatch.setitem(auth.config._data, "oidc_client_id", "client-id")
    monkeypatch.setitem(auth.config._data, "oidc_client_secret", "")

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
        monkeypatch.setitem(auth.config._data, "oidc_provider_url", "https://issuer.example")
        monkeypatch.setitem(auth.config._data, "oidc_client_id", "client-id")
        monkeypatch.setitem(auth.config._data, "oidc_client_secret", "secret")
        mw = auth.AuthMiddleware(None)
        for path in ["/health", "/auth/login", "/auth/callback", "/auth/logout", "/auth/me", "/api/config"]:
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
        monkeypatch.setitem(auth.config._data, "oidc_provider_url", "https://issuer.example")
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
        monkeypatch.setitem(auth.config._data, "oidc_provider_url", "https://issuer.example")
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
        monkeypatch.setitem(auth.config._data, "oidc_provider_url", "https://issuer.example")
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

        result = asyncio.run(mw.dispatch(request, call_next))
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
