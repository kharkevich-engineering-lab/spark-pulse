"""The OIDC login flow, driven end to end against a real provider.

``tests/test_e2e_oidc.py`` starts ``oidc_provider_mock`` and stops at the
callback URL, with a comment explaining that the TestClient cannot follow the
redirect because the callback exchanges the code over httpx. So the exchange —
the half where the credentials actually move — was never executed by any test,
and two bugs lived in it.

They are only visible against a provider that does **not** put its endpoints
where a naive guess would put them. ``oidc_provider_mock`` serves
``/oauth2/token`` and ``/userinfo`` off its issuer, which is exactly what the
callback hardcoded, so the flow appeared to work. Keycloak — the provider
``auth.py``'s own docstring uses as its example — serves
``/protocol/openid-connect/token`` instead, and login there was broken.

:func:`discovery_front` is that difference, made testable: a real provider,
behind a discovery document that advertises its endpoints somewhere other than
the guess. Anything that reads the document works against both; anything that
guesses works against neither.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import httpx
import oidc_provider_mock
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from spark_pulse import auth
from spark_pulse.app import create_app
from spark_pulse.config import config

USER = "alice@example.com"


@pytest.fixture
def provider():
    """A real OIDC provider: authorize, token, userinfo, and a JWKS."""
    with oidc_provider_mock.run_server_in_thread() as server:
        base = f"http://localhost:{server.server_port}"
        httpx.put(
            f"{base}/users/{USER.replace('@', '%40')}",
            json={"claims": {"email": USER, "name": "Alice"}},
        )
        yield base


@pytest.fixture
def discovery_front(provider):
    """The provider, behind a discovery document that moves its endpoints.

    Serves only ``/.well-known/openid-configuration``, carrying the *real*
    endpoints as absolute URLs. A client that reads the document reaches the
    provider; a client that appends ``/oauth2/token`` to this base reaches a
    404, which is what Keycloak's users were getting.
    """
    document = httpx.get(f"{provider}/.well-known/openid-configuration").json()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 — BaseHTTPRequestHandler's name
            if self.path != "/.well-known/openid-configuration":
                self.send_error(404)
                return
            body = json.dumps(document).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):  # keep the suite's output readable
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def client(discovery_front, monkeypatch):
    """The app, pointed at the front, with the session store left clean."""
    monkeypatch.setenv("SPARK_PULSE_AUTH_ENABLED", "true")
    monkeypatch.setitem(config._data, "oidc_provider_url", discovery_front)
    monkeypatch.setitem(config._data, "oidc_client_id", "spark-pulse-test")
    monkeypatch.setitem(config._data, "oidc_client_secret", "test-secret")
    auth._active_tokens.clear()
    auth._pending_states.clear()
    with TestClient(create_app(), raise_server_exceptions=False) as test_client:
        yield test_client
    auth._active_tokens.clear()
    auth._pending_states.clear()


def _authorize(location: str) -> str:
    """Play the browser and the human: follow the redirect, log Alice in.

    Returns the callback URL the provider redirects back to, code and all.
    """
    parsed = urlparse(location)
    query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    endpoint = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    answer = httpx.post(
        endpoint, params=query, data={"sub": USER}, follow_redirects=False
    )
    assert answer.status_code == 302, answer.text[:400]
    return answer.headers["location"]


def _callback_path(callback_url: str) -> str:
    parsed = urlparse(callback_url)
    return f"{parsed.path}?{parsed.query}"


# ── The flow ────────────────────────────────────────────────────────────────


def test_the_whole_login_flow_completes_and_leaves_a_usable_session(client):
    """Login, authorize, callback, cookie, authenticated request.

    The assertion that matters is that this runs at all: the code exchange and
    the userinfo call both happen here, against endpoints the app had to read
    out of the discovery document to find.
    """
    start = client.get("/auth/login", follow_redirects=False)
    assert start.status_code == 307

    callback_url = _authorize(start.headers["location"])
    done = client.get(_callback_path(callback_url), follow_redirects=False)

    assert done.status_code == 302, done.text[:400]
    assert done.headers["location"] == "/"
    assert "token" in done.cookies or "token" in client.cookies

    me = client.get("/auth/me")
    assert me.status_code == 200
    assert me.json()["authenticated"] is True

    # And the session is good for an ordinary protected call, which is the
    # only thing any of this was for.
    assert client.get("/api/settings").status_code == 200


def test_the_session_names_the_user_the_provider_authenticated(client):
    start = client.get("/auth/login", follow_redirects=False)
    callback_url = _authorize(start.headers["location"])
    client.get(_callback_path(callback_url), follow_redirects=False)

    user = client.get("/auth/me").json()["user"]

    # ``sub`` is the one claim OIDC guarantees at the top level; the shape of
    # the rest is the provider's business, not ours.
    assert user.get("sub") == USER


def test_a_second_login_does_not_reuse_the_first_ones_state(client):
    """Each login gets its own single-use state, and the old one dies with it."""
    first = client.get("/auth/login", follow_redirects=False)
    second = client.get("/auth/login", follow_redirects=False)

    first_state = parse_qs(urlparse(first.headers["location"]).query)["state"][0]
    second_state = parse_qs(urlparse(second.headers["location"]).query)["state"][0]
    assert first_state != second_state

    callback_url = _authorize(second.headers["location"])
    assert client.get(_callback_path(callback_url)).status_code in (200, 302)
    # The first state was never redeemed and is still live; the second is spent.
    assert auth._consume_state(second_state) is None
    assert auth._consume_state(first_state) is not None


def test_a_replayed_callback_is_refused(client):
    """The same callback URL twice is a replay, and the state is single-use."""
    start = client.get("/auth/login", follow_redirects=False)
    callback_url = _authorize(start.headers["location"])
    path = _callback_path(callback_url)

    assert client.get(path, follow_redirects=False).status_code == 302
    replayed = client.get(path, follow_redirects=False)

    assert replayed.status_code == 400
    assert "state" in replayed.json()["detail"].lower()


def test_logout_ends_the_session_the_flow_created(client):
    start = client.get("/auth/login", follow_redirects=False)
    callback_url = _authorize(start.headers["location"])
    client.get(_callback_path(callback_url), follow_redirects=False)
    assert client.get("/auth/me").status_code == 200

    assert client.post("/auth/logout").status_code == 200

    assert client.get("/auth/me").status_code == 401


# ── The ID token, checked ───────────────────────────────────────────────────


@pytest.fixture
def signing():
    """An RSA key, the JWKS that publishes it, and a server serving that JWKS.

    Real keys and a real HTTP fetch, because ``_verify_id_token`` fetches the
    key set itself and a monkeypatched fetch would test the assertion logic
    while skipping the part that talks to the provider.
    """
    from authlib.jose import JsonWebKey

    key = JsonWebKey.generate_key("RSA", 2048, is_private=True)
    public = json.dumps(
        {"keys": [key.as_dict(is_private=False, alg="RS256", use="sig")]}
    )

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = public.encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield key, f"http://127.0.0.1:{server.server_port}/jwks"
    finally:
        server.shutdown()
        server.server_close()


def _mint(key, **claims) -> str:
    from authlib.jose import JsonWebToken

    import time as _time

    payload = {
        "iss": "https://issuer.test",
        "aud": "spark-pulse-test",
        "sub": USER,
        "exp": int(_time.time()) + 300,
        "iat": int(_time.time()),
        "nonce": "the-right-nonce",
    }
    payload.update(claims)
    return JsonWebToken(["RS256"]).encode({"alg": "RS256"}, payload, key).decode()


@pytest.fixture
def verify(signing, monkeypatch):
    """``_verify_id_token`` bound to the signing fixture's issuer and keys."""
    import asyncio

    key, jwks_uri = signing
    monkeypatch.setitem(config._data, "oidc_client_id", "spark-pulse-test")
    document = {"issuer": "https://issuer.test", "jwks_uri": jwks_uri}

    def run(token: str, nonce: str = "the-right-nonce"):
        return asyncio.run(auth._verify_id_token(token, document, nonce))

    return key, run


def test_a_well_formed_id_token_verifies(verify):
    key, run = verify

    claims = run(_mint(key))

    assert claims["sub"] == USER


def test_an_id_token_for_another_login_is_refused(verify):
    """The nonce is the only thing tying a token to the login that asked.

    Without it a token captured from one login replays into another.
    """
    key, run = verify

    with pytest.raises(HTTPException) as caught:
        run(_mint(key, nonce="somebody-elses-nonce"))

    assert caught.value.status_code == 401
    assert "different login" in str(caught.value.detail)


def test_an_id_token_for_another_client_is_refused(verify):
    key, run = verify

    with pytest.raises(HTTPException) as caught:
        run(_mint(key, aud="some-other-app"))

    assert caught.value.status_code == 401


def test_an_id_token_from_another_issuer_is_refused(verify):
    key, run = verify

    with pytest.raises(HTTPException) as caught:
        run(_mint(key, iss="https://attacker.test"))

    assert caught.value.status_code == 401


def test_an_id_token_signed_by_a_stranger_is_refused(verify):
    """The signature is the whole assertion; an unknown key is not a provider."""
    from authlib.jose import JsonWebKey

    _key, run = verify
    stranger = JsonWebKey.generate_key("RSA", 2048, is_private=True)

    with pytest.raises(HTTPException) as caught:
        run(_mint(stranger))

    assert caught.value.status_code == 401


def test_an_expired_id_token_is_refused(verify):
    import time as _time

    key, run = verify

    with pytest.raises(HTTPException) as caught:
        run(_mint(key, exp=int(_time.time()) - 3600))

    assert caught.value.status_code == 401
