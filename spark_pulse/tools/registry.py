"""The control node's image registry, and digest-preserving seeding.

Multi-node distribution used to be ``docker save | ssh docker load``. Measured
on a real image, that round trip **changes the image digest** and comes back
with an empty ``RepoDigests``, and it puts 2.15 times more bytes on the wire
because the save format stores layers uncompressed. We deploy by digest-pinned
reference, so a node seeded that way cannot resolve the reference it was
handed, and its next pull re-downloads every layer instead of reusing them.

The replacement is a registry on the control node. Nodes pull from it over the
LAN, **anonymously**: no upstream credential is ever copied to a node, which is
the point of the arrangement (section 3.4 of the cluster-agent plan). The
upstream credential lives in the control node's ``secrets.json`` and is read
here only to authenticate the control node's own fetch.

Two shapes, both verified to preserve the digest byte-identically at every hop:

``local`` (the default)
    A full registry, seeded deliberately with :func:`seed`. Nothing appears in
    it that we did not put there, so a fixed cluster gets determinism.

``proxy``
    A pull-through cache holding the upstream credential and serving the LAN
    anonymously. Convenient, but distribution's cached-blob expiry has a
    long-standing sharp edge, so the cache TTL is pinned to never expire and
    this is not the default.

Everything that touches the outside world goes through two injectable seams —
a :class:`CommandRunner` for the docker and skopeo CLIs, and a head-request
callable for the registry HTTP API — so ``spark_pulse.mock.registry`` runs
*this* module's logic over simulated transports rather than reimplementing it.

Not yet done, and it needs the second machine to test: the registry serves
plain HTTP on the LAN, so a node's Docker daemon will refuse it until
``<control>:5000`` is in that node's ``insecure-registries``. Nothing here
writes a node's ``daemon.json``; that belongs with the node registry and
pre-flight of the plan's phase C, which is where a node's configuration first
becomes something we own.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple, Protocol

from spark_pulse.config import config
from spark_pulse.tools.docker import split_ref

logger = logging.getLogger(__name__)

# ── Modes ────────────────────────────────────────────────────────────────────

#: A full registry on the control node, seeded deliberately by :func:`seed`.
MODE_LOCAL = "local"
#: A pull-through cache of an upstream registry.
MODE_PROXY = "proxy"
MODES = (MODE_LOCAL, MODE_PROXY)

#: Explicit fetch-once beats a cache whose blob expiry has a sharp edge, and a
#: fixed cluster wants determinism, so a full registry is what we default to.
DEFAULT_MODE = MODE_LOCAL

REGISTRY_IMAGE = "registry:3"
CONTAINER_NAME = "spark-pulse-registry"
DEFAULT_PORT = 5000
#: The port the registry listens on *inside* its container.
INTERNAL_PORT = 5000
#: ``proxy.ttl = 0`` disables blob expiry outright. A cached layer that expires
#: mid-cluster is exactly the non-determinism this whole path exists to avoid.
PROXY_TTL_NEVER = "0"
DEFAULT_UPSTREAM = "https://ghcr.io"

#: Secret keys holding the upstream credential. **Control node only.** These
#: are never read into anything a node receives; see :func:`seed`.
UPSTREAM_USER_SECRET = "registry_username"
UPSTREAM_PASSWORD_SECRET = "registry_password"

_MANIFEST_TYPES = (
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json",
)
ACCEPT_MANIFESTS = ", ".join(_MANIFEST_TYPES)

_DIGEST_RE = re.compile(r"^[a-z0-9]+:[0-9a-f]{32,}$")


class RegistryError(RuntimeError):
    """The registry container could not be driven."""


class SeedError(RuntimeError):
    """An image reached the local registry under a different digest.

    Loud on purpose. A silently re-digested image is the defect this module
    replaces, and a node deploying by digest would fail far from the cause.
    """


# ── The command seam ─────────────────────────────────────────────────────────


class CommandResult(NamedTuple):
    """What running one CLI command produced."""

    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def message(self) -> str:
        return (self.stderr or self.stdout or "").strip()


class CommandRunner(Protocol):
    """Runs one argv and reports the result. Never raises for exit status."""

    def __call__(self, argv: list[str], timeout: int = 120) -> CommandResult: ...


def subprocess_runner(argv: list[str], timeout: int = 120) -> CommandResult:
    """Run ``argv`` with :mod:`subprocess`, capturing both streams."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        return CommandResult(127, "", str(exc))
    except subprocess.SubprocessError as exc:
        return CommandResult(1, "", str(exc))
    return CommandResult(proc.returncode, proc.stdout or "", proc.stderr or "")


def _run(runner: CommandRunner | None) -> CommandRunner:
    return runner or subprocess_runner


# ── The HTTP seam ────────────────────────────────────────────────────────────


class HeadRequest(Protocol):
    """A HEAD against the registry API, returning ``(status, headers)``."""

    def __call__(
        self, url: str, headers: dict[str, str], timeout: int = 30
    ) -> tuple[int, dict[str, str]]: ...


def httpx_head(
    url: str, headers: dict[str, str], timeout: int = 30
) -> tuple[int, dict[str, str]]:
    """HEAD ``url`` with httpx, following the registry's redirects."""
    import httpx

    try:
        response = httpx.head(
            url, headers=headers, timeout=timeout, follow_redirects=True
        )
    except httpx.HTTPError as exc:
        raise RegistryError(f"registry HEAD {url} failed: {exc}") from exc
    return response.status_code, {k.lower(): v for k, v in response.headers.items()}


# ── Settings ─────────────────────────────────────────────────────────────────


def _default_data_dir() -> Path:
    return Path.home() / ".local" / "share" / "spark-pulse" / "registry"


@dataclass(frozen=True, slots=True)
class RegistrySettings:
    """Where the control node's registry lives and how it behaves."""

    mode: str = DEFAULT_MODE
    address: str = ""
    port: int = DEFAULT_PORT
    image: str = REGISTRY_IMAGE
    container_name: str = CONTAINER_NAME
    data_dir: str = ""
    upstream: str = DEFAULT_UPSTREAM
    ttl: str = PROXY_TTL_NEVER

    @property
    def base(self) -> str:
        """The registry base every node composes its pull reference against."""
        return f"{self.address}:{self.port}"

    @property
    def url(self) -> str:
        """The registry API root, as the control node reaches it."""
        return f"http://{self.base}"

    @property
    def publish(self) -> str:
        """The ``-p`` argument: bound to the cluster-facing address only."""
        return f"{self.address}:{self.port}:{INTERNAL_PORT}"

    @property
    def is_proxy(self) -> bool:
        return self.mode == MODE_PROXY


def cluster_address() -> str:
    """The address workers reach the control node on.

    Discovery is best effort; loopback is the honest answer when nothing else
    is known, and a single-node cluster works with it.
    """
    from spark_pulse import tools

    try:
        found = tools.discovery.detect_local_ip()
    except Exception as exc:  # pragma: no cover — discovery is best effort
        logger.debug("could not detect the cluster-facing address: %s", exc)
        found = None
    return str(found or "127.0.0.1")


def load_settings(overrides: dict[str, Any] | None = None) -> RegistrySettings:
    """Registry settings from ``config.yaml``/``settings.json``, plus overrides."""
    data: dict[str, Any] = {}
    try:
        data.update(config.image_registry or {})
    except Exception as exc:  # pragma: no cover — defensive
        logger.debug("could not read image_registry settings: %s", exc)
    data.update(overrides or {})

    mode = str(data.get("mode") or DEFAULT_MODE).strip().lower()
    if mode not in MODES:
        logger.warning("unknown registry mode %r; using %s", mode, DEFAULT_MODE)
        mode = DEFAULT_MODE
    address = str(data.get("address") or "").strip() or cluster_address()
    data_dir = str(data.get("data_dir") or "").strip() or str(_default_data_dir())
    return RegistrySettings(
        mode=mode,
        address=address,
        port=int(data.get("port") or DEFAULT_PORT),
        image=str(data.get("image") or REGISTRY_IMAGE),
        container_name=str(data.get("container_name") or CONTAINER_NAME),
        data_dir=data_dir,
        upstream=str(data.get("upstream") or DEFAULT_UPSTREAM),
        ttl=str(data.get("ttl") or PROXY_TTL_NEVER),
    )


def upstream_credentials() -> tuple[str, str]:
    """The upstream credential, **which stays on this machine**.

    Read only to authenticate the control node's own fetch (``skopeo
    --src-creds`` or the proxy's remote login). Nothing in this module puts it
    into a command that runs on another node, and nothing hands it to the API.
    """
    try:
        user = config.get_secret(UPSTREAM_USER_SECRET)
        password = config.get_secret(UPSTREAM_PASSWORD_SECRET)
    except Exception as exc:  # pragma: no cover — defensive
        logger.debug("could not read registry credentials: %s", exc)
        return "", ""
    return str(user or ""), str(password or "")


# ── The three-field reference ────────────────────────────────────────────────


def repository_path(repository: str) -> str:
    """The path part of ``repository``, with any registry host removed.

    ``ghcr.io/owner/repo`` -> ``owner/repo``. The host is what changes per
    node; the path and the digest do not, which is why they are stored apart.
    """
    repository = (repository or "").strip().strip("/")
    if not repository:
        return ""
    head, sep, tail = repository.partition("/")
    if sep and ("." in head or ":" in head or head == "localhost"):
        return tail
    return repository


def registry_host(repository: str) -> str:
    """The registry host in ``repository``, or "" for a bare path."""
    head, sep, _ = (repository or "").strip().partition("/")
    if sep and ("." in head or ":" in head or head == "localhost"):
        return head
    return ""


@dataclass(frozen=True, slots=True)
class ImageLocation:
    """An image as three fields rather than one opaque reference.

    A node that pulled from the control node records
    ``<control>:5000/owner/repo@sha256:D`` while the control node knows it as
    ``ghcr.io/owner/repo@sha256:D``. Same content, same digest, different host
    — so the host is a field and the reference is composed per node.
    """

    registry_base: str
    repository: str
    digest: str

    def reference(self, registry_base: str = "") -> str:
        """The pull reference for ``registry_base`` (default: this one's)."""
        base = (registry_base or self.registry_base or "").strip().strip("/")
        name = f"{base}/{self.repository}" if base else self.repository
        return f"{name}@{self.digest}" if self.digest else name

    def to_dict(self) -> dict[str, str]:
        return {
            "registry_base": self.registry_base,
            "repository": self.repository,
            "digest": self.digest,
        }


def is_digest(value: str) -> bool:
    """Whether ``value`` is a content digest rather than a tag."""
    return bool(_DIGEST_RE.match((value or "").strip()))


def location_for(ref: str, digest: str = "") -> ImageLocation:
    """The three-field location of ``ref``, preferring an explicit ``digest``."""
    repository, reference = split_ref(ref)
    resolved = (digest or "").strip() or (reference if is_digest(reference) else "")
    return ImageLocation(
        registry_base=registry_host(repository),
        repository=repository_path(repository),
        digest=resolved,
    )


def pull_reference(ref: str, digest: str = "", registry_base: str = "") -> str:
    """Compose the reference a node at ``registry_base`` should pull.

    With no base and no digest this is ``ref`` unchanged, so a caller can use
    it unconditionally.
    """
    location = location_for(ref, digest)
    if not location.digest:
        if not registry_base:
            return ref
        return f"{registry_base}/{location.repository}:{split_ref(ref)[1]}"
    return location.reference(registry_base)


def seed_tag(digest: str, reference: str = "") -> str:
    """The tag the seeded copy carries in the local registry.

    A registry cannot be pushed to by digest, so a digest-pinned source needs
    a tag; ``sha256-<hex>`` is the conventional spelling and stays stable, so
    re-seeding overwrites rather than accumulating.
    """
    if reference and not is_digest(reference):
        return reference
    algo, _, hexpart = (digest or reference or "").partition(":")
    return f"{algo}-{hexpart}" if hexpart else "seeded"


# ── Container lifecycle ──────────────────────────────────────────────────────


def _inspect_container(
    settings: RegistrySettings, runner: CommandRunner | None
) -> dict[str, Any] | None:
    """The registry container's record, or None when it does not exist."""
    result = _run(runner)(
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            f"name=^{settings.container_name}$",
            "--format",
            "{{json .}}",
        ],
        30,
    )
    if not result.ok:
        raise RegistryError(f"could not query the registry container: {result.message}")
    for line in (result.stdout or "").strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return None


def _is_running(record: dict[str, Any] | None) -> bool:
    if not record:
        return False
    state = str(record.get("State") or record.get("Status") or "").lower()
    return state.startswith("running") or state.startswith("up")


def _env_args(settings: RegistrySettings) -> list[str]:
    """Registry environment: proxying, TTL and deletion."""
    env = [
        "-e",
        f"REGISTRY_HTTP_ADDR=0.0.0.0:{INTERNAL_PORT}",
        "-e",
        "REGISTRY_STORAGE_DELETE_ENABLED=true",
    ]
    if not settings.is_proxy:
        return env
    user, password = upstream_credentials()
    env += [
        "-e",
        f"REGISTRY_PROXY_REMOTEURL={settings.upstream}",
        # Never expire: a blob evicted mid-cluster is the sharp edge that keeps
        # proxy mode off the default path.
        "-e",
        f"REGISTRY_PROXY_TTL={settings.ttl}",
    ]
    if user:
        env += ["-e", f"REGISTRY_PROXY_USERNAME={user}"]
    if password:
        env += ["-e", f"REGISTRY_PROXY_PASSWORD={password}"]
    return env


def status(
    settings: RegistrySettings | None = None,
    runner: CommandRunner | None = None,
) -> dict[str, Any]:
    """What the registry is, where it listens, and whether it is up."""
    settings = settings or load_settings()
    try:
        record = _inspect_container(settings, runner)
        error: str | None = None
    except RegistryError as exc:
        record, error = None, str(exc)
    user, password = upstream_credentials()
    return {
        "mode": settings.mode,
        "default_mode": DEFAULT_MODE,
        "running": _is_running(record),
        "exists": record is not None,
        "container": settings.container_name,
        "image": settings.image,
        "address": settings.address,
        "port": settings.port,
        "base": settings.base,
        "url": settings.url,
        "data_dir": settings.data_dir,
        "upstream": settings.upstream if settings.is_proxy else "",
        "proxy_ttl": settings.ttl if settings.is_proxy else "",
        # The whole point of the arrangement, stated where the API can see it.
        "nodes_need_credentials": False,
        "credentials_on_control_node": bool(user or password),
        "error": error,
    }


def start(
    settings: RegistrySettings | None = None,
    runner: CommandRunner | None = None,
) -> dict[str, Any]:
    """Start the registry, idempotently.

    Already running is success. Present but stopped is started in place, so a
    reboot does not lose the seeded blobs.
    """
    settings = settings or load_settings()
    run = _run(runner)
    record = _inspect_container(settings, runner)
    if _is_running(record):
        return status(settings, runner)
    if record is not None:
        result = run(["docker", "start", settings.container_name], 60)
        if not result.ok:
            raise RegistryError(
                f"could not start the registry container: {result.message}"
            )
        return status(settings, runner)

    Path(settings.data_dir).expanduser().mkdir(parents=True, exist_ok=True)
    argv = [
        "docker",
        "run",
        "-d",
        "--restart",
        "unless-stopped",
        "--name",
        settings.container_name,
        # Bound to the cluster-facing address: the registry is for the LAN,
        # not for anything that can reach any other interface.
        "-p",
        settings.publish,
        "-v",
        f"{Path(settings.data_dir).expanduser()}:/var/lib/registry",
        *_env_args(settings),
        settings.image,
    ]
    result = run(argv, 300)
    if not result.ok:
        raise RegistryError(f"could not start the registry: {result.message}")
    return status(settings, runner)


def stop(
    settings: RegistrySettings | None = None,
    runner: CommandRunner | None = None,
) -> dict[str, Any]:
    """Stop and remove the registry container, idempotently."""
    settings = settings or load_settings()
    record = _inspect_container(settings, runner)
    if record is None:
        return status(settings, runner)
    result = _run(runner)(["docker", "rm", "-f", settings.container_name], 120)
    if not result.ok:
        raise RegistryError(f"could not stop the registry: {result.message}")
    return status(settings, runner)


def ensure_running(
    settings: RegistrySettings | None = None,
    runner: CommandRunner | None = None,
) -> dict[str, Any]:
    """Start the registry unless it is already up."""
    settings = settings or load_settings()
    state = status(settings, runner)
    if state.get("running"):
        return state
    return start(settings, runner)


# ── Digest verification ──────────────────────────────────────────────────────


def manifest_digest(
    repository: str,
    reference: str,
    settings: RegistrySettings | None = None,
    http: HeadRequest | None = None,
) -> str:
    """The digest the local registry reports for ``repository:reference``.

    This is the registry's own answer (``Docker-Content-Digest``), not
    something we recomputed, so comparing it to the advertised digest is a
    real end-to-end check of the copy.
    """
    settings = settings or load_settings()
    head = http or httpx_head
    url = f"{settings.url}/v2/{repository}/manifests/{reference}"
    code, headers = head(url, {"Accept": ACCEPT_MANIFESTS}, 60)
    if code == 404:
        return ""
    if code >= 400:
        raise RegistryError(f"registry returned {code} for {repository}:{reference}")
    return str(headers.get("docker-content-digest") or "").strip()


# ── Seeding ──────────────────────────────────────────────────────────────────


def _skopeo_available(runner: CommandRunner | None) -> bool:
    return _run(runner)(["skopeo", "--version"], 30).ok


def _copy_with_skopeo(
    ref: str,
    destination: str,
    runner: CommandRunner | None,
    timeout: int,
) -> None:
    """``skopeo copy --all --preserve-digests`` — every hop byte-identical.

    ``--all`` carries the whole index (every architecture) rather than one
    manifest, and ``--preserve-digests`` refuses the copy outright rather than
    re-encoding anything, which is what makes the destination digest equal to
    the source one instead of merely similar.
    """
    argv = [
        "skopeo",
        "copy",
        "--all",
        "--preserve-digests",
        # The local registry speaks plain HTTP on the LAN.
        "--dest-tls-verify=false",
    ]
    user, password = upstream_credentials()
    if user or password:
        # Control node only: this authenticates *our* fetch from upstream.
        argv += ["--src-creds", f"{user}:{password}"]
    argv += [f"docker://{ref}", f"docker://{destination}"]
    result = _run(runner)(argv, timeout)
    if not result.ok:
        raise SeedError(f"skopeo copy of {ref} failed: {result.message}")


def _copy_with_docker(
    ref: str,
    destination: str,
    runner: CommandRunner | None,
    timeout: int,
) -> None:
    """Fallback: pull on the control node, then tag and push into the registry.

    Weaker than skopeo — the daemon may re-encode a manifest it did not
    produce — which is exactly why :func:`seed` verifies the result instead of
    assuming it.
    """
    run = _run(runner)
    for argv, phase in (
        (["docker", "pull", ref], "pull"),
        (["docker", "tag", ref, destination], "tag"),
        (["docker", "push", destination], "push"),
    ):
        result = run(argv, timeout)
        if not result.ok:
            raise SeedError(f"docker {phase} of {ref} failed: {result.message}")


def seed(
    ref: str,
    digest: str = "",
    *,
    settings: RegistrySettings | None = None,
    runner: CommandRunner | None = None,
    http: HeadRequest | None = None,
    timeout: int = 3600,
) -> dict[str, Any]:
    """Place ``ref`` in the local registry with its digest intact.

    Args:
        ref: The upstream reference, tagged or digest-pinned.
        digest: What the engine index advertises for it. When given it is the
            authority the copy is checked against.
        settings: Registry settings; loaded from config when omitted.
        runner: Command seam (simulation, tests).
        http: HEAD seam for the verification (simulation, tests).
        timeout: Per-command timeout, in seconds.

    Returns:
        The three-field location plus the composed ``pull_ref`` and which tool
        did the copy.

    Raises:
        SeedError: The copy failed, or the registry reports a different digest
            than the one advertised. Never a warning: a re-digested image
            breaks every digest-pinned deploy that follows it.
    """
    ref = (ref or "").strip()
    if not ref:
        raise ValueError("ref is required")
    settings = settings or load_settings()
    ensure_running(settings, runner)

    repository, reference = split_ref(ref)
    path = repository_path(repository)
    if not path:
        raise ValueError(f"could not parse a repository out of {ref!r}")
    advertised = (digest or "").strip() or (reference if is_digest(reference) else "")
    tag = seed_tag(advertised, reference)
    destination = f"{settings.base}/{path}:{tag}"

    if _skopeo_available(runner):
        tool = "skopeo"
        _copy_with_skopeo(ref, destination, runner, timeout)
    else:
        tool = "docker"
        _copy_with_docker(ref, destination, runner, timeout)

    observed = manifest_digest(path, tag, settings, http)
    if not observed:
        raise SeedError(
            f"{destination} is not in the local registry after seeding {ref}"
        )
    if advertised and observed != advertised:
        raise SeedError(
            f"seeding {ref} changed its digest: the index advertises "
            f"{advertised} but {settings.base} reports {observed}"
        )
    resolved = advertised or observed

    # The deploy is by digest, so the digest-pinned form is what has to
    # resolve — a tag that happens to work is not the thing being asserted.
    by_digest = manifest_digest(path, resolved, settings, http)
    if by_digest != resolved:
        raise SeedError(
            f"{settings.base}/{path}@{resolved} does not resolve in the local "
            f"registry after seeding {ref} (got {by_digest or 'nothing'})"
        )

    location = ImageLocation(
        registry_base=settings.base, repository=path, digest=resolved
    )
    logger.info("seeded %s into %s as %s", ref, settings.base, location.reference())
    return {
        "ref": ref,
        "source": location_for(ref, resolved).to_dict(),
        **location.to_dict(),
        "pull_ref": location.reference(),
        "tag": tag,
        "tool": tool,
        "verified": True,
        # Restated per seed because it is the property the mode exists for.
        "nodes_need_credentials": False,
    }


def node_reference(
    ref: str,
    digest: str = "",
    *,
    settings: RegistrySettings | None = None,
    http: HeadRequest | None = None,
) -> str:
    """The reference a worker should pull for ``ref``.

    The seeded copy when the local registry actually holds it, and ``ref``
    unchanged when it does not — a deploy must not be rewritten to point at a
    registry with nothing in it. Any failure to ask is treated as "not there",
    because guessing wrong here fails the deploy on the node.
    """
    ref = (ref or "").strip()
    if not ref:
        return ref
    try:
        settings = settings or load_settings()
        location = location_for(ref, digest)
        if not location.repository:
            return ref
        tag = seed_tag(location.digest, split_ref(ref)[1])
        observed = manifest_digest(location.repository, tag, settings, http)
        if not observed:
            return ref
        if location.digest and observed != location.digest:
            logger.warning(
                "local registry holds %s for %s but %s was asked for; "
                "leaving the reference alone",
                observed,
                ref,
                location.digest,
            )
            return ref
        return ImageLocation(settings.base, location.repository, observed).reference()
    except Exception as exc:  # noqa: BLE001 — an unreachable registry is a no-op
        logger.debug("could not compose a registry reference for %s: %s", ref, exc)
        return ref


def describe(
    ref: str, digest: str = "", settings: RegistrySettings | None = None
) -> dict[str, Any]:
    """The three fields and the composed reference for ``ref``, without I/O."""
    settings = settings or load_settings()
    upstream = location_for(ref, digest)
    local = ImageLocation(
        registry_base=settings.base,
        repository=upstream.repository,
        digest=upstream.digest,
    )
    return {
        "ref": ref,
        **local.to_dict(),
        "pull_ref": local.reference(),
        "upstream": upstream.to_dict(),
        "nodes_need_credentials": False,
    }


__all__ = [
    "ACCEPT_MANIFESTS",
    "CONTAINER_NAME",
    "DEFAULT_MODE",
    "DEFAULT_PORT",
    "DEFAULT_UPSTREAM",
    "INTERNAL_PORT",
    "MODES",
    "MODE_LOCAL",
    "MODE_PROXY",
    "PROXY_TTL_NEVER",
    "REGISTRY_IMAGE",
    "CommandResult",
    "CommandRunner",
    "HeadRequest",
    "ImageLocation",
    "RegistryError",
    "RegistrySettings",
    "SeedError",
    "cluster_address",
    "describe",
    "ensure_running",
    "httpx_head",
    "is_digest",
    "load_settings",
    "location_for",
    "manifest_digest",
    "node_reference",
    "pull_reference",
    "registry_host",
    "repository_path",
    "seed",
    "seed_tag",
    "start",
    "status",
    "stop",
    "subprocess_runner",
    "upstream_credentials",
]
