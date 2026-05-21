from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from spark_pulse import auth


class DummyRequest:
    def __init__(self, user=None):
        self.state = SimpleNamespace(user=user)


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


def test_get_current_user_returns_user():
    user = {"name": "Alice"}

    out = auth.get_current_user(DummyRequest(user=user))

    assert out == user


def test_get_current_user_raises_when_missing_user():
    with pytest.raises(HTTPException) as exc:
        auth.get_current_user(DummyRequest())

    assert exc.value.status_code == 401
