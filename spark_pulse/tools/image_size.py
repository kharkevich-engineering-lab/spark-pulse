"""How many bytes a pull of an image moves, asked of its registry (#210).

The pre-flight used to say *"about 25.0 GB has to transfer"* for any image no
node held yet: nothing could report the size, so it fell back to an assumed
figure for a Spark engine image and printed it as an estimate. The lab's
llama.cpp image is a fraction of that.

A pull transfers the compressed layers of the manifest for the puller's
platform, and the registry states each one's size. So the answer is the
image index for the reference, the manifest it lists for the node's platform,
and the sum of that manifest's layers and config — two small requests, and
for a digest-pinned reference the answer never changes, so it is cached for
the life of the process.

Anonymous, like the pulls the nodes make of a public engine image: the
registry's own ``WWW-Authenticate`` challenge says where to get a pull token.
Anything that does not answer — a private registry, no network, a platform the
index does not carry — is ``None``, which the caller reports as unknown rather
than replacing with a guess.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Any

from spark_pulse.tools import registry

logger = logging.getLogger(__name__)

__all__ = ["transfer_bytes", "platform_manifest", "manifest_bytes"]

#: Seconds each registry request may take. The pre-flight waits on this.
TIMEOUT = 5.0

#: Every manifest shape a registry may answer with, index or single.
ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)

#: Docker Hub names itself one way in a reference and serves from another.
_DOCKER_HUB = {"", "docker.io", "index.docker.io"}
_DOCKER_HUB_API = "registry-1.docker.io"

_CHALLENGE = re.compile(r'(\w+)="([^"]*)"')

_cache: dict[tuple[str, str], int] = {}
_lock = threading.Lock()


def platform_manifest(index: dict[str, Any], platform: str) -> str:
    """The digest of ``index``'s manifest for ``platform`` (``os/arch``).

    Empty when the index carries no build for it. Attestation entries
    (``unknown/unknown``) are never a match. Pure.
    """
    os_name, _, arch = platform.partition("/")
    arch, _, variant = arch.partition("/")
    for entry in index.get("manifests") or []:
        wanted = entry.get("platform") or {}
        if wanted.get("os") != os_name or wanted.get("architecture") != arch:
            continue
        if variant and wanted.get("variant") and wanted["variant"] != variant:
            continue
        return str(entry.get("digest") or "")
    return ""


def manifest_bytes(manifest: dict[str, Any]) -> int:
    """What pulling a single-platform manifest moves: its layers and config."""
    layers = sum(int(layer.get("size") or 0) for layer in manifest.get("layers") or [])
    return layers + int((manifest.get("config") or {}).get("size") or 0)


def transfer_bytes(ref: str, platform: str) -> int | None:
    """Bytes a node of ``platform`` downloads to pull ``ref``; None if unknown."""
    if not ref or not platform:
        return None
    key = (ref, platform)
    with _lock:
        if key in _cache:
            return _cache[key]
    try:
        size = _ask(ref, platform)
    except Exception as exc:  # noqa: BLE001 — any failure is "unknown"
        logger.debug("could not size %s for %s: %s", ref, platform, exc)
        return None
    _, reference = registry.split_ref(ref)
    if size is not None and registry.is_digest(reference):
        # A digest names fixed content; a tag may move, so it is asked again.
        with _lock:
            _cache[key] = size
    return size


def _ask(ref: str, platform: str) -> int | None:
    import httpx

    repository, reference = registry.split_ref(ref)
    host = registry.registry_host(repository)
    api = _DOCKER_HUB_API if host in _DOCKER_HUB else host
    path = registry.repository_path(repository)
    if host in _DOCKER_HUB and "/" not in path:
        path = f"library/{path}"

    with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as client:
        token: list[str] = []

        def get(name: str) -> dict[str, Any]:
            url = f"https://{api}/v2/{path}/manifests/{name}"
            headers = {"Accept": ACCEPT}
            if token:
                headers["Authorization"] = f"Bearer {token[0]}"
            response = client.get(url, headers=headers)
            if response.status_code == 401 and not token:
                token.append(
                    _token(client, response.headers.get("www-authenticate", ""))
                )
                headers["Authorization"] = f"Bearer {token[0]}"
                response = client.get(url, headers=headers)
            response.raise_for_status()
            return response.json()

        manifest = get(reference or "latest")
        if manifest.get("manifests") is not None:
            child = platform_manifest(manifest, platform)
            if not child:
                return None
            manifest = get(child)
        size = manifest_bytes(manifest)
        return size or None


def _token(client: Any, challenge: str) -> str:
    """A pull token from the realm the registry's challenge names."""
    scheme, _, rest = challenge.partition(" ")
    if scheme.lower() != "bearer":
        raise RuntimeError(f"registry wants {scheme or 'no'} auth, not a bearer token")
    params = dict(_CHALLENGE.findall(rest))
    realm = params.pop("realm", "")
    if not realm:
        raise RuntimeError("registry challenge names no token realm")
    response = client.get(realm, params=params)
    response.raise_for_status()
    body = response.json()
    return str(body.get("token") or body.get("access_token") or "")
