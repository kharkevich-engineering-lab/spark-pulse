"""Sizing an image's pull from its registry (#210)."""

from __future__ import annotations

import importlib

import pytest

# The real module: under SIMULATION_MODE ``spark_pulse.tools.image_size`` is the
# mock, which asks nothing.
image_size = importlib.import_module("spark_pulse.tools.image_size")

DIGEST = "sha256:" + "ab" * 32
REF = f"ghcr.io/lab/engine/llama-cpp@{DIGEST}"

INDEX = {
    "manifests": [
        {"digest": "sha256:amd", "platform": {"os": "linux", "architecture": "amd64"}},
        {
            "digest": "sha256:arm",
            "platform": {"os": "linux", "architecture": "arm64", "variant": "v8"},
        },
        {
            "digest": "sha256:att",
            "platform": {"os": "unknown", "architecture": "unknown"},
        },
    ]
}


@pytest.fixture(autouse=True)
def _empty_cache():
    image_size._cache.clear()
    yield
    image_size._cache.clear()


def test_the_index_entry_for_the_platform_is_picked():
    assert image_size.platform_manifest(INDEX, "linux/amd64") == "sha256:amd"
    assert image_size.platform_manifest(INDEX, "linux/arm64") == "sha256:arm"
    assert image_size.platform_manifest(INDEX, "linux/arm64/v8") == "sha256:arm"
    assert image_size.platform_manifest(INDEX, "linux/riscv64") == ""


def test_a_pull_moves_the_layers_and_the_config():
    manifest = {"config": {"size": 10}, "layers": [{"size": 100}, {"size": 1000}]}
    assert image_size.manifest_bytes(manifest) == 1110


def test_a_digest_is_asked_once(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        image_size, "_ask", lambda ref, platform: calls.append(platform) or 42
    )
    assert image_size.transfer_bytes(REF, "linux/amd64") == 42
    assert image_size.transfer_bytes(REF, "linux/amd64") == 42
    assert calls == ["linux/amd64"]


def test_a_tag_is_asked_again_because_it_can_move(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        image_size, "_ask", lambda ref, platform: calls.append(ref) or 7
    )
    image_size.transfer_bytes("ghcr.io/lab/engine:latest", "linux/amd64")
    image_size.transfer_bytes("ghcr.io/lab/engine:latest", "linux/amd64")
    assert len(calls) == 2


def test_a_registry_that_does_not_answer_is_unknown(monkeypatch):
    def boom(*_a):
        raise OSError("no network")

    monkeypatch.setattr(image_size, "_ask", boom)
    assert image_size.transfer_bytes(REF, "linux/amd64") is None
    assert image_size._cache == {}


def test_no_platform_is_unknown_without_asking(monkeypatch):
    monkeypatch.setattr(image_size, "_ask", lambda *_a: pytest.fail("asked"))
    assert image_size.transfer_bytes(REF, "") is None


def test_the_index_then_the_platform_manifest_is_read(monkeypatch):
    """Over the registry protocol, with the bearer challenge answered."""
    import httpx

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.host == "ghcr.io" and request.url.path == "/token":
            return httpx.Response(200, json={"token": "t0k"})
        if request.headers.get("authorization") != "Bearer t0k":
            return httpx.Response(
                401,
                headers={
                    "www-authenticate": 'Bearer realm="https://ghcr.io/token",'
                    'service="ghcr.io",scope="repository:lab/engine/llama-cpp:pull"'
                },
            )
        if request.url.path.endswith(DIGEST):
            return httpx.Response(200, json=INDEX)
        if request.url.path.endswith("sha256:amd"):
            return httpx.Response(
                200, json={"config": {"size": 5}, "layers": [{"size": 95}]}
            )
        return httpx.Response(404)

    real_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw),
    )

    assert image_size.transfer_bytes(REF, "linux/amd64") == 100
    assert "/v2/lab/engine/llama-cpp/manifests/sha256:amd" in seen
    assert image_size.transfer_bytes(REF, "linux/s390x") is None
