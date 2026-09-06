"""The boundaries a browser can reach, and what may cross them.

Each test here corresponds to a hole the review found open, so each one names
what it would cost rather than only what it asserts.

The theme is that this control plane runs on an operator's own machine and,
with ``auth_enabled`` false by default, answers every caller. That makes the
*browser* the boundary: whatever page the operator happens to have open is the
one thing on the network that can reach ``localhost:8100`` with intent. Two
mechanisms keep it out, and both were absent:

* CORS may not reflect an arbitrary origin, or a page on another site can read
  the answers.
* A state-changing request carrying a foreign ``Origin`` must be refused, or a
  page on another site can provoke writes whether or not it can read them.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from spark_pulse import app as app_module
from spark_pulse import config as config_module
from spark_pulse.app import create_app
from spark_pulse.config import config


@pytest.fixture(autouse=True)
def private_config_files(tmp_path, monkeypatch):
    """Never write to the developer's own ``~/.config/spark-pulse``."""
    monkeypatch.setattr(config_module, "_SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(config_module, "_SECRETS_PATH", tmp_path / "secrets.json")
    snapshot = dict(config._data)
    yield
    config._data.clear()
    config._data.update(snapshot)


@pytest.fixture
def client():
    with TestClient(create_app()) as test_client:
        yield test_client


# ── CORS ────────────────────────────────────────────────────────────────────


def test_a_foreign_origin_is_not_reflected_back(client):
    """The headline finding.

    Starlette reflects the caller's own ``Origin`` when the wildcard is
    combined with ``allow_credentials``, so ``allow_origins=["*"]`` did not
    mean "no credentials, no reading" — it meant every page the operator
    visits could read this API's answers.
    """
    response = client.get("/health", headers={"Origin": "https://evil.example"})

    assert response.status_code == 200
    allowed = response.headers.get("access-control-allow-origin")
    assert allowed != "https://evil.example"
    assert allowed != "*"


def test_the_uis_own_origin_is_allowed(client):
    """And the fix must not break the product it protects."""
    origin = f"http://localhost:{config.webui_port}"

    response = client.get("/health", headers={"Origin": origin})

    assert response.headers.get("access-control-allow-origin") == origin


def test_the_dev_server_origin_is_allowed(client):
    """``run-dev-server.sh`` serves the SPA from Vite and proxies /api here,
    so the browser's Origin is Vite's. Forgetting it breaks every dev run."""
    response = client.get("/health", headers={"Origin": "http://localhost:3000"})

    assert response.headers.get("access-control-allow-origin") == (
        "http://localhost:3000"
    )


def test_a_foreign_preflight_is_not_granted(client):
    response = client.options(
        "/api/settings",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "PUT",
        },
    )

    assert response.headers.get("access-control-allow-origin") is None


# ── Cross-origin writes ─────────────────────────────────────────────────────


def test_a_write_from_a_foreign_origin_is_refused(client):
    """The other half: a form post needs no CORS grant to *happen*.

    Reading the answer is blocked by CORS; performing the write is not. Only
    an origin check on the request itself stops a page on another site from
    stopping deployments in the operator's browser.
    """
    response = client.put(
        "/api/settings",
        json={"job_retention_days": 1},
        headers={"Origin": "https://evil.example"},
    )

    assert response.status_code == 403
    assert "evil.example" in response.json()["detail"]


def test_a_write_from_the_uis_own_origin_is_allowed(client):
    origin = f"http://localhost:{config.webui_port}"

    response = client.put(
        "/api/settings", json={"job_retention_days": 3}, headers={"Origin": origin}
    )

    assert response.status_code == 200


def test_a_write_with_no_origin_is_allowed(client):
    """``curl``, the MCP client and the test client are not CSRF victims.

    A browser attaches ``Origin`` to every cross-origin request and to every
    unsafe same-origin one, so "no Origin" is exactly the set of callers that
    cannot be driven by a hostile page.
    """
    response = client.put("/api/settings", json={"job_retention_days": 5})

    assert response.status_code == 200


def test_a_read_from_a_foreign_origin_is_not_itself_refused(client):
    """Safe methods are not the origin check's business — CORS is.

    Refusing them here would break nothing an attacker cares about and would
    break link previews and health probes.
    """
    response = client.get("/health", headers={"Origin": "https://evil.example"})

    assert response.status_code == 200


# ── The settings write surface ──────────────────────────────────────────────


def test_settings_cannot_turn_authentication_off(client):
    """``update`` writes straight into settings.json, and ``auth_enabled`` is
    read from settings.json. Without an allowlist the settings endpoint is the
    way past every other check in the system."""
    response = client.put("/api/settings", json={"auth_enabled": False})

    assert response.status_code == 400
    assert "auth_enabled" in response.json()["detail"]


def test_settings_cannot_write_a_secret(client):
    response = client.put("/api/settings", json={"oidc_client_secret": "stolen"})

    assert response.status_code == 400
    assert config.oidc_client_secret != "stolen"


def test_settings_still_accepts_what_it_reports(client):
    """The allowlist is the response's own field set, so a round trip works."""
    current = client.get("/api/settings").json()
    editable = {
        key: value
        for key, value in current.items()
        if key not in {"env_managed", "engines", "engine_indexes"}
    }

    response = client.put("/api/settings", json=editable)

    assert response.status_code == 200


def test_settings_json_is_not_world_readable(client, tmp_path):
    """It holds ``oidc_client_secret`` whenever OIDC is configured here."""
    client.put("/api/settings", json={"job_retention_days": 9})
    path = config_module._SETTINGS_PATH

    assert path.exists()
    assert json.loads(path.read_text())["job_retention_days"] == 9
    assert path.stat().st_mode & 0o077 == 0


# ── Unmatched API paths ─────────────────────────────────────────────────────


def test_an_unknown_api_path_is_a_404_not_the_spa(client):
    """The SPA catch-all answers every method, so a mistyped or removed
    endpoint answered with 200 and a page of HTML. A client cannot tell that
    from success, and the ones that suffer are the ones least able to
    complain: an MCP tool call or a script parsing HTML as though it were the
    JSON it asked for."""
    for method, path in (
        ("post", "/api/deployment"),  # the real one is /api/deployments
        ("delete", "/api/nodes"),
        ("get", "/api/no-such-thing"),
        ("post", "/auth/nope"),
    ):
        response = getattr(client, method)(path)
        assert response.status_code == 404, f"{method.upper()} {path}"
        assert response.headers["content-type"].startswith("application/json")


def test_a_client_side_route_still_gets_the_app(client, tmp_path, monkeypatch):
    """And the catch-all still does the job it exists for.

    The UI directory is stood up here rather than assumed. It is build output:
    present on a developer's machine that has run the frontend build, absent
    in the CI job that runs these tests, so a test that just asks for a route
    asserts "the frontend happens to be built" in one place and raises in the
    other.
    """
    index = tmp_path / "index.html"
    index.write_text("<!doctype html><title>Spark Pulse</title>")
    monkeypatch.setattr(app_module, "_UI_DIR", tmp_path)
    monkeypatch.setattr(app_module, "_INDEX_FILE", index)

    response = client.get("/monitoring")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


# ── The MCP endpoint's own credential ───────────────────────────────────────


def test_mcp_is_open_when_no_token_is_configured(client):
    """Unset keeps the previous behaviour exactly: the session governs."""
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}

    response = client.post("/mcp", json=body)

    assert response.status_code == 200
    assert "result" in response.json()


def test_mcp_requires_the_token_once_one_is_configured(monkeypatch):
    """``mcp_api_token`` was declared in config.yaml and read by nothing, so an
    operator who set one got no protection and no sign that they had not.

    It matters because an MCP client running as a program cannot complete an
    OIDC redirect, so without this there is no way for it to authenticate at
    all."""
    monkeypatch.setitem(config._data, "mcp_api_token", "s3cret-token")
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    with TestClient(create_app()) as guarded:
        assert guarded.post("/mcp", json=body).status_code == 401
        assert (
            guarded.post(
                "/mcp", json=body, headers={"Authorization": "Bearer wrong"}
            ).status_code
            == 401
        )
        allowed = guarded.post(
            "/mcp", json=body, headers={"Authorization": "Bearer s3cret-token"}
        )
        assert allowed.status_code == 200
        assert "result" in allowed.json()
