"""The two startup postures that must never silently serve.

This control plane answers unauthenticated callers whenever ``auth_enabled`` is
false (the default), and the "browser is the boundary" model only constrains
cross-site *browser* requests. So two things, and only two, keep the mutating
API off the network:

* the socket is bound to loopback, and
* if auth is turned on, it is actually configured.

Both used to fail quietly. A ``0.0.0.0`` default bind exposed everything to the
LAN; auth turned on with a fumbled secret read as "not configured" and passed
every request through — auth failing *open*. These tests hold the guards that
turn each into a refuse-to-start.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner
from starlette.responses import JSONResponse

from spark_pulse import auth
from spark_pulse import cli
from spark_pulse import config as config_module
from spark_pulse.app import create_app
from spark_pulse.config import (
    ALLOW_INSECURE_BIND_ENV,
    BIND_HOST_ENV,
    AuthConfigError,
    InsecureBindError,
    assert_bind_is_safe,
    bind_is_loopback,
    config,
)


@pytest.fixture(autouse=True)
def private_config_files(tmp_path, monkeypatch):
    """Never touch the developer's own ``~/.config/spark-pulse``, and restore
    the shared ``config._data`` this suite mutates in place."""
    monkeypatch.setattr(config_module, "_SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(config_module, "_SECRETS_PATH", tmp_path / "secrets.json")
    snapshot = dict(config._data)
    yield
    config._data.clear()
    config._data.update(snapshot)


def _configure_oidc(monkeypatch):
    monkeypatch.setitem(config._data, "oidc_provider_url", "https://issuer.example")
    monkeypatch.setitem(config._data, "oidc_client_id", "client-id")
    monkeypatch.setitem(config._data, "oidc_client_secret", "secret")


# ── bind_is_loopback ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "host, expected",
    [
        ("127.0.0.1", True),
        ("::1", True),
        ("localhost", True),
        ("", True),  # unset → the safe default
        (None, True),
        ("0.0.0.0", False),  # all IPv4 interfaces
        ("::", False),  # all IPv6 interfaces
        ("192.168.1.10", False),
        ("not-an-address", False),  # a name we cannot prove local
    ],
)
def test_bind_is_loopback_classifies_hosts(host, expected):
    assert bind_is_loopback(host) is expected


# ── The bind guard ───────────────────────────────────────────────────────────


def test_a_non_loopback_bind_with_auth_off_is_refused(monkeypatch):
    """The headline: ``0.0.0.0`` with auth off exposes every mutating endpoint
    to anyone on the LAN, so the app refuses to start."""
    monkeypatch.setenv("SPARK_PULSE_AUTH_ENABLED", "false")
    monkeypatch.setenv(BIND_HOST_ENV, "0.0.0.0")

    with pytest.raises(InsecureBindError):
        create_app()


def test_a_loopback_bind_with_auth_off_still_starts(monkeypatch):
    """The default posture — loopback, no auth — must keep working."""
    monkeypatch.setenv("SPARK_PULSE_AUTH_ENABLED", "false")
    monkeypatch.setenv(BIND_HOST_ENV, "127.0.0.1")

    assert create_app() is not None


def test_an_unset_bind_host_is_treated_as_loopback(monkeypatch):
    monkeypatch.setenv("SPARK_PULSE_AUTH_ENABLED", "false")
    monkeypatch.delenv(BIND_HOST_ENV, raising=False)

    assert create_app() is not None


def test_a_non_loopback_bind_with_auth_on_is_allowed(monkeypatch):
    """Exposure is only a problem while the API is unauthenticated."""
    monkeypatch.setenv("SPARK_PULSE_AUTH_ENABLED", "true")
    monkeypatch.setenv(BIND_HOST_ENV, "0.0.0.0")
    _configure_oidc(monkeypatch)

    assert create_app() is not None


def test_the_insecure_bind_escape_hatch(monkeypatch):
    """An operator who means it can opt in explicitly."""
    monkeypatch.setenv("SPARK_PULSE_AUTH_ENABLED", "false")
    monkeypatch.setenv(BIND_HOST_ENV, "0.0.0.0")

    with pytest.raises(InsecureBindError):
        assert_bind_is_safe("0.0.0.0")

    monkeypatch.setenv(ALLOW_INSECURE_BIND_ENV, "1")
    assert create_app() is not None  # no raise


# ── The auth-config guard (fail closed) ──────────────────────────────────────


@pytest.mark.parametrize(
    "present",
    [
        (),  # nothing configured
        ("oidc_provider_url",),
        ("oidc_provider_url", "oidc_client_id"),  # secret missing
    ],
)
def test_auth_on_with_incomplete_oidc_refuses_to_start(monkeypatch, present):
    """Auth on but half-configured is the fail-*open* case: refuse to start
    rather than serve every request unauthenticated."""
    monkeypatch.setenv("SPARK_PULSE_AUTH_ENABLED", "true")
    values = {
        "oidc_provider_url": "https://issuer.example",
        "oidc_client_id": "client-id",
        "oidc_client_secret": "secret",
    }
    for key in present:
        monkeypatch.setitem(config._data, key, values[key])

    with pytest.raises(AuthConfigError):
        create_app()


def test_auth_on_with_complete_oidc_starts(monkeypatch):
    monkeypatch.setenv("SPARK_PULSE_AUTH_ENABLED", "true")
    _configure_oidc(monkeypatch)

    assert create_app() is not None


def test_auth_off_ignores_oidc_config(monkeypatch):
    """Auth off is open by design — a missing OIDC config is not a problem."""
    monkeypatch.setenv("SPARK_PULSE_AUTH_ENABLED", "false")

    assert create_app() is not None


# ── The middleware fails closed too ──────────────────────────────────────────


def _run_dispatch(mw, request):
    called = []

    async def call_next(_):
        called.append(True)
        return MagicMock()

    result = asyncio.run(mw.dispatch(request, call_next))
    return result, called


def _protected_request():
    request = MagicMock()
    request.url.path = "/api/recipes"
    request.headers = {}
    request.cookies = {}
    return request


def test_middleware_gates_on_the_flag_not_on_oidc_completeness(monkeypatch):
    """Belt-and-suspenders behind the refuse-to-start guard: even if a
    half-configured process reached the middleware, a protected path is
    rejected, not waved through. Keying on ``_oidc_configured()`` was how an
    incomplete config silently disabled the wall."""
    monkeypatch.setenv("SPARK_PULSE_AUTH_ENABLED", "true")
    monkeypatch.setitem(config._data, "oidc_provider_url", "")
    monkeypatch.setitem(config._data, "oidc_client_id", "")
    monkeypatch.setitem(config._data, "oidc_client_secret", "")
    assert auth._oidc_configured() is False  # the fail-open trigger

    result, called = _run_dispatch(auth.AuthMiddleware(None), _protected_request())

    assert called == []  # request did not pass through
    assert isinstance(result, JSONResponse)
    assert result.status_code == 401


def test_middleware_allows_everything_when_auth_off(monkeypatch):
    monkeypatch.setenv("SPARK_PULSE_AUTH_ENABLED", "false")

    result, called = _run_dispatch(auth.AuthMiddleware(None), _protected_request())

    assert called == [True]
    assert result is not None


# ── The CLI refuses fast ─────────────────────────────────────────────────────


def test_cli_start_refuses_a_public_bind_with_auth_off(monkeypatch):
    """The operator gets a clean message, not a uvicorn import-time traceback,
    and uvicorn is never exec'd."""
    monkeypatch.setenv("SPARK_PULSE_AUTH_ENABLED", "false")
    # Keep the env write inside the command from leaking into the test process.
    monkeypatch.setattr(cli.os, "environ", dict(cli.os.environ))
    runner = CliRunner()

    with pytest.MonkeyPatch.context() as mp:
        exec_called = []
        mp.setattr(cli.os, "execvp", lambda *a, **k: exec_called.append(a))
        result = runner.invoke(cli.main, ["start", "--host", "0.0.0.0", "--dry-run"])

    assert result.exit_code == 1
    assert "auth" in result.output.lower()
    assert exec_called == []


def test_cli_start_allows_a_loopback_bind_with_auth_off(monkeypatch):
    monkeypatch.setenv("SPARK_PULSE_AUTH_ENABLED", "false")
    monkeypatch.setattr(cli.os, "environ", dict(cli.os.environ))
    runner = CliRunner()

    result = runner.invoke(cli.main, ["start", "--host", "127.0.0.1", "--dry-run"])

    assert result.exit_code == 0
    assert "Would run: uvicorn" in result.output
