"""Simulated control-node registry — the real logic over invented transports.

There is no second implementation here. :mod:`spark_pulse.tools.registry`
routes everything outward through two seams, a command runner and a HEAD
callable, so simulation supplies those and the argv building, the digest
comparison and the failure messages under test are the production ones.

:class:`SimulatedRegistry` is deliberately faithful about the one property the
whole module exists for: a copy preserves the digest. Flip
:attr:`SimulatedRegistry.rewrite_digest` and it behaves like the
``docker save | ssh docker load`` path it replaced — the digest changes — which
is what a test asserts :func:`seed` refuses.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from spark_pulse.tools.registry import (  # noqa: F401 — shared machinery
    ACCEPT_MANIFESTS as ACCEPT_MANIFESTS,
    CONTAINER_NAME as CONTAINER_NAME,
    DEFAULT_MODE as DEFAULT_MODE,
    DEFAULT_PORT as DEFAULT_PORT,
    DEFAULT_UPSTREAM as DEFAULT_UPSTREAM,
    INTERNAL_PORT as INTERNAL_PORT,
    LOOPBACK as LOOPBACK,
    MODE_LOCAL as MODE_LOCAL,
    MODE_PROXY as MODE_PROXY,
    MODES as MODES,
    PROXY_TTL_NEVER as PROXY_TTL_NEVER,
    REGISTRY_IMAGE as REGISTRY_IMAGE,
    SKOPEO_CONTAINER as SKOPEO_CONTAINER,
    SKOPEO_IMAGE as SKOPEO_IMAGE,
    CommandResult,
    CommandRunner as CommandRunner,
    HeadRequest,
    httpx_head as httpx_head,
    subprocess_runner as subprocess_runner,
    ImageLocation as ImageLocation,
    RegistryError as RegistryError,
    RegistrySettings,
    SeedError as SeedError,
    cluster_address as cluster_address,
    describe as _real_describe,
    ensure_running as _real_ensure_running,
    is_digest as is_digest,
    load_settings as load_settings,
    location_for as location_for,
    loopback_reference as loopback_reference,
    manifest_digest as _real_manifest_digest,
    node_reference as _real_node_reference,
    pull_reference as pull_reference,
    registry_host as registry_host,
    relay_target as relay_target,
    repository_path as repository_path,
    seed as _real_seed,
    seed_tag as seed_tag,
    split_ref,
    start as _real_start,
    status as _real_status,
    stop as _real_stop,
    upstream_credentials as upstream_credentials,
)


def _digest_for(ref: str) -> str:
    """The digest a copy of ``ref`` carries.

    The simulated host's own answer where it has one, so a copy preserves what
    the control node already held rather than inventing a second identity —
    which is the property the real path is built to guarantee.
    """
    repository, reference = split_ref(ref)
    if is_digest(reference):
        return reference
    try:
        from spark_pulse.mock.docker import _get_service

        info = _get_service().image_info(ref) or {}
        for entry in info.get("repo_digests") or []:
            digest = str(entry).partition("@")[2]
            if digest:
                return digest
    except Exception:  # pragma: no cover — simulation is best effort
        pass
    return (
        "sha256:" + uuid.uuid5(uuid.NAMESPACE_URL, f"{repository}:{reference}").hex * 2
    )


class SimulatedRegistry:
    """One control node's registry: a container record and some manifests."""

    def __init__(
        self,
        skopeo: bool = True,
        rewrite_digest: bool = False,
        skopeo_container: bool = True,
    ):
        """Simulate the registry container and its API.

        Args:
            skopeo: Whether ``skopeo`` is on the simulated control node. False
                exercises skopeo's own image run over docker.
            skopeo_container: Whether that image can be run. False (with
                ``skopeo`` False) exercises the ``docker pull``/tag/push
                fallback.
            rewrite_digest: Re-digest every copy, the way ``docker save |
                docker load`` did. Seeding must then fail loudly.
        """
        self.skopeo = skopeo
        self.skopeo_container = skopeo_container
        self.rewrite_digest = rewrite_digest
        #: Every argv the module asked for, for assertions.
        self.commands: list[list[str]] = []
        #: ``repository -> {tag or digest: digest}``.
        self.manifests: dict[str, dict[str, str]] = {}
        self._container: dict[str, Any] | None = None
        self._binds: list[str] = []
        self._staged: tuple[str, str] | None = None

    # ── The command seam ─────────────────────────────────────────────────

    def run(self, argv: list[str], timeout: int = 120) -> CommandResult:
        """Answer one docker or skopeo invocation."""
        self.commands.append(list(argv))
        if not argv:
            return CommandResult(127, "", "no command")
        if argv[0] == "skopeo":
            return self._skopeo(argv)
        if argv[0] == "docker":
            return self._docker(argv)
        return CommandResult(127, "", f"not simulated: {argv[0]}")

    def _skopeo(self, argv: list[str]) -> CommandResult:
        if len(argv) > 1 and argv[1] == "--version":
            if not self.skopeo:
                return CommandResult(127, "", "skopeo: command not found")
            return CommandResult(0, "skopeo version 1.16.0\n", "")
        if len(argv) > 1 and argv[1] == "copy":
            refs = [a[len("docker://") :] for a in argv if a.startswith("docker://")]
            if len(refs) != 2:
                return CommandResult(1, "", f"bad skopeo copy: {argv}")
            self._store(refs[0], refs[1])
            return CommandResult(0, "Copying image\n", "")
        return CommandResult(1, "", f"unsupported skopeo: {argv}")

    def _docker(self, argv: list[str]) -> CommandResult:
        verb = argv[1] if len(argv) > 1 else ""
        if verb == "ps":
            if self._container is None:
                return CommandResult(0, "", "")
            return CommandResult(0, json.dumps(self._container) + "\n", "")
        if verb == "run" and SKOPEO_IMAGE in argv:
            if not self.skopeo_container:
                return CommandResult(125, "", f"Unable to find image '{SKOPEO_IMAGE}'")
            return self._skopeo(["skopeo", *argv[argv.index(SKOPEO_IMAGE) + 1 :]])
        if verb == "run":
            binds = [argv[i + 1] for i, arg in enumerate(argv) if arg == "-p"]
            self._binds = binds
            self._container = {
                "Names": argv[argv.index("--name") + 1],
                "Image": argv[-1],
                "State": "running",
                "Status": "Up 1 second",
                "Ports": self._ports(binds),
            }
            return CommandResult(0, uuid.uuid4().hex[:12] + "\n", "")
        if verb == "start":
            if self._container is None:
                return CommandResult(1, "", "No such container")
            self._container["State"] = "running"
            self._container["Ports"] = self._ports(self._binds)
            return CommandResult(0, argv[-1] + "\n", "")
        if verb == "rm":
            if self._container is None:
                return CommandResult(1, "", "No such container")
            self._container = None
            return CommandResult(0, argv[-1] + "\n", "")
        if verb == "pull":
            return CommandResult(0, f"Downloaded {argv[-1]}\n", "")
        if verb == "tag":
            self._store(argv[2], argv[3], defer=True)
            return CommandResult(0, "", "")
        if verb == "push":
            self._commit(argv[-1])
            return CommandResult(0, f"Pushed {argv[-1]}\n", "")
        return CommandResult(1, "", f"unsupported docker verb: {verb}")

    @staticmethod
    def _ports(binds: list[str]) -> str:
        """``docker ps``'s ``Ports`` column for these ``-p`` arguments."""
        rendered = []
        for bind in binds:
            host, _, inner = bind.rpartition(":")
            rendered.append(f"{host}->{inner}/tcp")
        return ", ".join(rendered)

    def lose_bindings(self) -> None:
        """What a reboot did on the cluster: up, and published on nothing."""
        if self._container is not None:
            self._container["Ports"] = ""
            self._binds = []

    # ── The manifest store ───────────────────────────────────────────────

    def _store(self, source: str, destination: str, defer: bool = False) -> None:
        """Record what ``destination`` resolves to after copying ``source``."""
        digest = _digest_for(source)
        if self.rewrite_digest:
            # What save/load did: same bytes, new identity.
            digest = (
                "sha256:" + uuid.uuid5(uuid.NAMESPACE_URL, "reloaded" + digest).hex * 2
            )
        if defer:
            self._staged = (destination, digest)
            return
        self._commit(destination, digest)

    def _commit(self, destination: str, digest: str = "") -> None:
        staged = self._staged
        if not digest and staged and staged[0] == destination:
            digest = staged[1]
        if not digest:
            digest = _digest_for(destination)
        repository, reference = split_ref(destination)
        path = repository_path(repository)
        entries = self.manifests.setdefault(path, {})
        entries[reference] = digest
        entries[digest] = digest

    def seed_manually(self, ref: str, digest: str = "") -> None:
        """Put an image in the registry without going through :func:`seed`."""
        location = location_for(ref, digest or _digest_for(ref))
        entries = self.manifests.setdefault(location.repository, {})
        tag = seed_tag(location.digest, split_ref(ref)[1])
        entries[tag] = location.digest
        entries[location.digest] = location.digest

    # ── The HTTP seam ────────────────────────────────────────────────────

    def head(
        self, url: str, headers: dict[str, str], timeout: int = 30
    ) -> tuple[int, dict[str, str]]:
        """Answer ``HEAD /v2/<repository>/manifests/<reference>``."""
        _ = headers, timeout
        path, _, reference = url.partition("/manifests/")
        repository = path.partition("/v2/")[2]
        digest = self.manifests.get(repository, {}).get(reference, "")
        if not digest:
            return 404, {}
        return 200, {"docker-content-digest": digest}


_default: SimulatedRegistry | None = None


def default_registry() -> SimulatedRegistry:
    """The process-wide simulated registry."""
    global _default
    if _default is None:
        _default = SimulatedRegistry()
    return _default


def reset() -> None:
    """Drop the simulated registry, and everything seeded into it."""
    global _default
    _default = None


def _settings(settings: RegistrySettings | None) -> RegistrySettings:
    """Simulation pins the address so the composed reference is predictable."""
    if settings is not None:
        return settings
    return load_settings({"address": cluster_address()})


# ── The real functions, over the simulated seams ─────────────────────────────


def status(
    settings: RegistrySettings | None = None, runner: Any | None = None
) -> dict[str, Any]:
    """Registry status, from the simulated container record."""
    return _real_status(_settings(settings), runner or default_registry().run)


def start(
    settings: RegistrySettings | None = None, runner: Any | None = None
) -> dict[str, Any]:
    """Start the simulated registry container."""
    return _real_start(_settings(settings), runner or default_registry().run)


def stop(
    settings: RegistrySettings | None = None, runner: Any | None = None
) -> dict[str, Any]:
    """Stop the simulated registry container."""
    return _real_stop(_settings(settings), runner or default_registry().run)


def ensure_running(
    settings: RegistrySettings | None = None, runner: Any | None = None
) -> dict[str, Any]:
    """Start the simulated registry unless it is already up."""
    return _real_ensure_running(_settings(settings), runner or default_registry().run)


def manifest_digest(
    repository: str,
    reference: str,
    settings: RegistrySettings | None = None,
    http: HeadRequest | None = None,
) -> str:
    """The digest the simulated registry reports."""
    return _real_manifest_digest(
        repository, reference, _settings(settings), http or default_registry().head
    )


def seed(
    ref: str,
    digest: str = "",
    *,
    settings: RegistrySettings | None = None,
    runner: Any | None = None,
    http: HeadRequest | None = None,
    timeout: int = 3600,
) -> dict[str, Any]:
    """Seed into the simulated registry, digest verification and all."""
    simulated = default_registry()
    return _real_seed(
        ref,
        digest,
        settings=_settings(settings),
        runner=runner or simulated.run,
        http=http or simulated.head,
        timeout=timeout,
    )


def node_reference(
    ref: str,
    digest: str = "",
    *,
    settings: RegistrySettings | None = None,
    http: HeadRequest | None = None,
) -> str:
    """The reference a simulated worker should pull."""
    return _real_node_reference(
        ref,
        digest,
        settings=_settings(settings),
        http=http or default_registry().head,
    )


def describe(
    ref: str, digest: str = "", settings: RegistrySettings | None = None
) -> dict[str, Any]:
    """The three fields and the composed reference, without I/O."""
    return _real_describe(ref, digest, _settings(settings))


__all__ = [
    "ACCEPT_MANIFESTS",
    "CONTAINER_NAME",
    "DEFAULT_MODE",
    "DEFAULT_PORT",
    "DEFAULT_UPSTREAM",
    "INTERNAL_PORT",
    "LOOPBACK",
    "MODES",
    "MODE_LOCAL",
    "MODE_PROXY",
    "PROXY_TTL_NEVER",
    "REGISTRY_IMAGE",
    "SKOPEO_CONTAINER",
    "SKOPEO_IMAGE",
    "ImageLocation",
    "RegistryError",
    "RegistrySettings",
    "SeedError",
    "SimulatedRegistry",
    "cluster_address",
    "default_registry",
    "describe",
    "ensure_running",
    "is_digest",
    "load_settings",
    "location_for",
    "loopback_reference",
    "manifest_digest",
    "node_reference",
    "pull_reference",
    "registry_host",
    "relay_target",
    "repository_path",
    "reset",
    "seed",
    "seed_tag",
    "start",
    "status",
    "stop",
    "upstream_credentials",
]
