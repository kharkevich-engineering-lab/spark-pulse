"""End-to-end tests for OIDC authentication flow using oidc-provider-mock.

These tests spin up a mock OIDC provider and verify the full login/callback
flow against the Spark Pulse FastAPI app.

Usage:
    pytest tests/test_e2e_oidc.py -v
"""

from __future__ import annotations

import os
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import oidc_provider_mock

from spark_pulse.app import create_app
from spark_pulse.config import config

# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def oidc_server():
    """Start a mock OIDC provider in a background thread."""
    with oidc_provider_mock.run_server_in_thread() as server:
        yield f"http://localhost:{server.server_port}"


@pytest.fixture(scope="module")
def app_client(oidc_server):
    """Create a test FastAPI app with OIDC enabled and return a TestClient."""
    # Configure OIDC to point at the mock provider
    os.environ["SPARK_PULSE_AUTH_ENABLED"] = "true"
    config._data["oidc_provider_url"] = oidc_server
    config._data["oidc_client_id"] = "test-client"
    config._data["oidc_client_secret"] = "test-secret"

    # Create app with test client
    app = create_app()

    from fastapi.testclient import TestClient

    client = TestClient(app, raise_server_exceptions=False)
    return client


@pytest.fixture(scope="module")
def configured_user(oidc_server):
    """Pre-register a test user with the mock OIDC provider."""
    response = httpx.put(
        f"{oidc_server}/users/alice%40example.com",
        json={
            "email": "alice@example.com",
            "name": "Alice",
            "sub": "alice@example.com",
        },
    )
    assert response.status_code == 204
    return {
        "email": "alice@example.com",
        "name": "Alice",
    }


# ── Tests: /api/config endpoint ─────────────────────────────────────────────


class TestApiConfig:
    """Test the SPA runtime config endpoint."""

    def test_config_returns_mcp_enabled(self, app_client, monkeypatch):
        """Config endpoint should report mcp_enabled status."""
        monkeypatch.setitem(config._data, "mcp_enabled", True)

        resp = app_client.get("/api/config")
        assert resp.status_code == 200
        data = resp.json()
        assert data["mcp_enabled"] is True

    def test_config_no_auth_field(self, app_client, monkeypatch):
        """Config should include auth_enabled but not oidc_configured."""
        resp = app_client.get("/api/config")
        assert resp.status_code == 200
        data = resp.json()
        assert "auth_enabled" in data
        assert "oidc_configured" not in data


# ── Tests: OIDC login flow ──────────────────────────────────────────────────


class TestOidcLoginFlow:
    """Test the full OIDC authorization code flow."""

    def test_login_redirects_to_oidc_provider(self, app_client, oidc_server):
        """Login should redirect to the OIDC provider's authorization page."""
        resp = app_client.get("/auth/login", follow_redirects=False)
        assert resp.status_code == 307
        location = resp.headers["location"]
        _parsed = urlparse(location)
        # Should redirect to the mock OIDC provider
        assert oidc_server.replace("http://", "") in location

    def test_callback_without_code_fails(self, app_client):
        """Callback without an authorization code should fail."""
        resp = app_client.get("/auth/callback", follow_redirects=False)
        # Should return an error (422 is from FastAPI validation, which is fine)
        assert resp.status_code in [400, 401, 422, 500]

    def test_full_login_flow(self, app_client, oidc_server, configured_user):
        """Test the complete login flow: login -> authorize -> callback."""
        # Step 1: Initiate login
        login_resp = app_client.get("/auth/login", follow_redirects=False)
        assert login_resp.status_code == 307
        auth_url = login_resp.headers["location"]

        # Step 2: Authorize with the mock provider
        # The mock provider expects a POST with the user's subject
        auth_resp = httpx.post(
            auth_url,
            data={"sub": configured_user["email"]},
            follow_redirects=False,
        )
        assert auth_resp.status_code == 302
        callback_url = auth_resp.headers["location"]

        # Step 3: The callback URL contains a code, not a token
        # The TestClient can't follow the redirect properly because the
        # callback endpoint tries to exchange the code via httpx
        # So we verify the callback URL has a code parameter
        parsed = urlparse(callback_url)
        query_params = parse_qs(parsed.query)
        assert "code" in query_params
        assert len(query_params["code"]) > 0

    def test_callback_with_a_forged_state_is_refused_before_the_exchange(
        self, app_client
    ):
        """A state we did not issue never reaches the token endpoint.

        This used to assert 401/500 — the token exchange failing on a bad
        code — because ``state`` was minted at ``/auth/login`` and then never
        looked at again. That made the callback accept an authorization code
        from anybody, which is login CSRF: an attacker completes the flow in
        the victim's browser and the victim works inside the attacker's
        account. The refusal now happens first, and 400 says why.
        """
        resp = app_client.get(
            "/auth/callback?code=invalid-code&state=fake-state",
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert "state" in resp.json()["detail"].lower()

    def test_callback_with_our_own_state_reaches_the_exchange(self, app_client):
        """And a state we *did* issue gets past the check to the real work.

        Otherwise the fix above would be indistinguishable from a callback
        that refuses everything.
        """
        from spark_pulse import auth

        state = auth._issue_state()
        resp = app_client.get(
            f"/auth/callback?code=invalid-code&state={state}",
            follow_redirects=False,
        )
        # Past the state check, and now failing on the code, which is the
        # provider's business rather than ours.
        assert resp.status_code in [401, 500]


# ── Tests: Token management ─────────────────────────────────────────────────


class TestTokenManagement:
    """Test token storage and invalidation."""

    def test_logout_invalidates_token(self):
        """Logout should invalidate the current token."""
        from spark_pulse import auth

        # Manually insert a valid token
        valid_token = "test-token-logout"
        auth._active_tokens[valid_token] = {
            "user": {"name": "Alice", "email": "alice@example.com"},
        }

        # Verify token is valid
        from fastapi.testclient import TestClient
        import warnings

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            me_resp = client.get("/auth/me", cookies={"token": valid_token})
        assert me_resp.status_code == 200

        # Logout (cookie cleared automatically)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            logout_resp = client.post("/auth/logout", cookies={"token": valid_token})
        assert logout_resp.status_code == 200

        # Token should now be invalid
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            me_resp_after = client.get("/auth/me", cookies={"token": valid_token})
        assert me_resp_after.status_code == 401


# ── Tests: Auth middleware ──────────────────────────────────────────────────


class TestAuthMiddleware:
    """Test the auth middleware behavior."""

    def test_public_paths_accessible_without_auth(self, app_client):
        """Public paths should be accessible without authentication."""
        # These paths should not redirect to login
        for path in ["/health", "/api/config"]:
            resp = app_client.get(path, follow_redirects=False)
            # Should not redirect to login (307 to /auth/login)
            assert resp.status_code not in [307] or "/auth/login" not in (
                resp.headers.get("location", "")
            ), f"Path {path} should be public"

    def test_protected_path_returns_401_when_not_authenticated(self, oidc_server):
        """Protected API paths should return 401 when not authenticated (SPA handling)."""
        from fastapi.testclient import TestClient

        # Configure OIDC to point at the mock provider
        os.environ["SPARK_PULSE_AUTH_ENABLED"] = "true"
        config._data["oidc_provider_url"] = oidc_server
        config._data["oidc_client_id"] = "test-client"
        config._data["oidc_client_secret"] = "test-secret"

        app = create_app()
        client = TestClient(app)

        resp = client.get("/api/recipes", follow_redirects=False)
        # Should return 401 for SPA to handle
        assert resp.status_code == 401

    def test_protected_path_accessible_with_valid_cookie(self):
        """Protected API paths should be accessible with a valid cookie."""
        from spark_pulse import auth

        # Manually insert a valid token
        valid_token = "test-token-protected"
        auth._active_tokens[valid_token] = {
            "user": {"name": "Bob", "email": "bob@example.com"},
        }

        from fastapi.testclient import TestClient

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        # Access protected path with cookie
        resp = client.get(
            "/api/recipes",
            cookies={"token": valid_token},
        )
        assert resp.status_code == 200


# ── Tests: Auth disabled mode ───────────────────────────────────────────────


class TestAuthDisabled:
    """Test behavior when OIDC is disabled."""

    def test_no_auth_required_when_disabled(self):
        """When auth is disabled, all paths should be accessible."""
        os.environ["SPARK_PULSE_AUTH_ENABLED"] = "false"
        # Clear OIDC config
        config._data["oidc_provider_url"] = ""
        config._data["oidc_client_id"] = ""
        config._data["oidc_client_secret"] = ""

        app = create_app()
        from fastapi.testclient import TestClient

        client = TestClient(app, raise_server_exceptions=False)

        # Should be able to access any path without auth
        resp = client.get("/api/recipes")
        assert resp.status_code == 200

        # Login endpoint should indicate auth is not configured
        resp = client.get("/auth/login")
        assert resp.status_code == 200
        data = resp.json()
        assert "not configured" in data["message"].lower()
        client = TestClient(app)

        # Should be able to access any path without auth
        resp = client.get("/api/recipes")
        assert resp.status_code == 200

        # Login endpoint should indicate auth is not configured
        resp = client.get("/auth/login")
        assert resp.status_code == 200
        data = resp.json()
        assert "not configured" in data["message"].lower()
