"""What gets copied to a node: one static binary.

**The decision, and what it replaces.** The agent used to ship as a gzipped
tar of the ``spark_pulse`` package plus every third-party module it imported —
grpcio, cryptography, the Docker SDK — run by the node's own ``python3``. That
worked exactly when one assumption held, and the assumption was written down
in this file: *"It is a DGX Spark running DGX OS, and so is every node it
enrolls: same architecture, same CPython minor version, so grpcio's and
cryptography's extension modules load."*

The assumption is false the moment the control plane is not itself a Spark. A
bundle built on a developer's Mac shipped ``cygrpc.cpython-314-darwin.so`` to
an aarch64 Linux node running CPython 3.12: wrong operating system, wrong
interpreter, and it could not load for either reason. The documented escape
hatch did not escape either — without the vendored runtime the node needs
grpcio, cryptography and the Docker SDK already installed, which means pip and
a network, which is precisely what an air-gapped fabric does not have.

So the agent is a single statically linked binary now, built for the *node's*
platform rather than assembled out of the control plane's environment. 3.5 MB
against 18 MB compressed, no interpreter, no shared objects, no matching of
anything. It runs on a machine with nothing on it but a kernel.

**Where the binary comes from.** ``scripts/build-agent.sh``, which builds it in
a container so the result does not depend on what is installed on the machine
that ran it. The bundle refuses to build without one rather than shipping
something that cannot run, and says which command produces it.

**Upgrades and version skew are unchanged.** The directory is named for the
version and a digest of its contents and ``current`` is a symlink, so
installing a newer agent writes a new directory and flips the symlink; the
previous bytes stay on disk and a rollback is one ``ln -sfn``. The identity
directory is outside the install root entirely, so neither an upgrade nor an
uninstall touches it.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import logging
import tarfile
from dataclasses import dataclass
from pathlib import Path

from spark_pulse.version import __version__

logger = logging.getLogger(__name__)

__all__ = [
    "AgentBundle",
    "DEFAULT_TARGET",
    "MissingAgentBinary",
    "VERIFY_COMMAND",
    "agent_binary",
    "binary_dir",
    "host_binary",
    "host_target",
    "build_bundle",
]

#: The platform every DGX Spark is. musl rather than gnu so the binary carries
#: its own libc and cannot be broken by the node's.
DEFAULT_TARGET = "aarch64-unknown-linux-musl"


#: Where a built binary is looked for. Package data, so a wheel can carry one;
#: gitignored, because a build artifact does not belong in the source tree.
def binary_dir() -> Path:
    return Path(__file__).resolve().parent / "bin"


class MissingAgentBinary(RuntimeError):
    """No agent binary for the target, and therefore nothing to install.

    Raised rather than falling back to anything. There is no second way to put
    an agent on a node any more, and an installer that appeared to work while
    shipping something unrunnable is worse than one that refuses.
    """


def agent_binary(target: str = DEFAULT_TARGET) -> Path:
    """The built agent for ``target``, or a refusal naming how to build it."""
    path = binary_dir() / f"spark-pulse-agent-{target}"
    if path.is_file():
        return path
    raise MissingAgentBinary(
        f"no agent binary for {target} at {path}. Build one with "
        f"`TARGET={target} ./scripts/build-agent.sh`, or install a spark-pulse "
        "release, which ships one."
    )


def host_target() -> str:
    """The triple for the machine this process is running on.

    Only two matter: a Spark, and a developer's laptop. On a Spark this is the
    same triple the bundle ships, which is why the control node can run the
    very binary it hands to its peers.
    """
    import platform

    machine = platform.machine().lower()
    arch = "aarch64" if machine in ("aarch64", "arm64") else machine
    if platform.system() == "Darwin":
        return f"{arch}-apple-darwin"
    return f"{arch}-unknown-linux-musl"


def host_binary() -> Path:
    """An agent this machine can execute, for the control node's own agent.

    Looked for by *this host's* triple only — never falling back to the triple
    the bundle ships. On a Spark those are the same string, so the fallback
    would buy nothing; anywhere else it would hand this machine a binary built
    for a different operating system and the failure would be an exec error
    with no clue in it. A cargo build is accepted last, purely so a developer
    who has just built the crate does not also have to package it.
    """
    candidates = [binary_dir() / f"spark-pulse-agent-{host_target()}"]
    crate = Path(__file__).resolve().parents[2] / "agent" / "target"
    candidates += [
        crate / "release" / "spark-pulse-agent",
        crate / "debug" / "spark-pulse-agent",
    ]
    for path in candidates:
        if path.is_file():
            return path
    raise MissingAgentBinary(
        f"no agent binary this machine can run ({host_target()}). Build one "
        "with `./scripts/build-agent.sh`, or `cd agent && cargo build` for a "
        "native one."
    )


#: Run on the node the moment the bundle is unpacked. ``--help`` loads the
#: whole program and exits 0, so a binary for the wrong architecture fails
#: here, named, with nothing installed and nothing started.
VERIFY_COMMAND = "--help"


@dataclass(frozen=True)
class AgentBundle:
    """The bytes to ship, and what they are."""

    version: str
    digest: str
    data: bytes
    target: str
    binary_size: int = 0

    @property
    def name(self) -> str:
        """The directory this unpacks into. Version *and* content, so two
        builds of one version never share a directory."""
        return f"{self.version}-{self.digest[:12]}"

    @property
    def size(self) -> int:
        return len(self.data)


def build_bundle(
    *,
    target: str = DEFAULT_TARGET,
    binary: Path | None = None,
    cache_dir: Path | None = None,
) -> AgentBundle:
    """Build (or reuse) the bundle for this control plane's version.

    ``cache_dir`` makes a second install of the same version free: the bundle
    is content-addressed, so a cached file is either exactly the bytes that
    would be built or it is not used.
    """
    source = binary or agent_binary(target)
    payload = source.read_bytes()

    buffer = io.BytesIO()
    # Every member's mtime is zero *and* so is the gzip header's, which is the
    # part that is easy to miss: `tarfile.open(mode="w:gz")` stamps the current
    # time into the gzip header, so two builds of identical inputs produced
    # different bytes, a different digest and therefore a different install
    # directory — and the bundle cache never hit. Wrapping an explicit
    # GzipFile with `mtime=0` is what makes the bundle content-addressed in
    # fact rather than only in intent.
    with gzip.GzipFile(fileobj=buffer, mode="wb", compresslevel=6, mtime=0) as raw:
        with tarfile.open(fileobj=raw, mode="w") as tar:
            _add_bytes(tar, "bin/spark-pulse-agent", payload, 0o755)
            _add_bytes(
                tar,
                "BUNDLE.json",
                json.dumps(
                    {
                        "version": __version__,
                        "target": target,
                        "binary_sha256": hashlib.sha256(payload).hexdigest(),
                        "binary_size": len(payload),
                    },
                    indent=2,
                    sort_keys=True,
                ).encode(),
                0o644,
            )
    data = buffer.getvalue()
    bundle = AgentBundle(
        version=__version__,
        digest=hashlib.sha256(data).hexdigest(),
        data=data,
        target=target,
        binary_size=len(payload),
    )
    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / f"spark-pulse-agent-{bundle.name}.tar.gz").write_bytes(data)
    return bundle


def _add_bytes(tar: tarfile.TarFile, arcname: str, data: bytes, mode: int) -> None:
    info = tarfile.TarInfo(arcname)
    info.size = len(data)
    info.mode = mode
    info.mtime = 0
    info.uid = info.gid = 0
    info.uname = info.gname = "root"
    tar.addfile(info, io.BytesIO(data))


def unpack_for_test(bundle: AgentBundle, destination: Path) -> Path:
    """Unpack a bundle locally. Used by the simulated node, and by tests."""
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(bundle.data), mode="r:gz") as tar:
        _extract_all(tar, destination)
    return destination


def _extract_all(tar: tarfile.TarFile, destination: Path) -> None:
    for member in tar.getmembers():
        target = (destination / member.name).resolve()
        if not str(target).startswith(str(destination.resolve())):
            raise RuntimeError(f"refusing path traversal in bundle: {member.name}")
        target.parent.mkdir(parents=True, exist_ok=True)
        extracted = tar.extractfile(member)
        if extracted is None:
            continue
        target.write_bytes(extracted.read())
        target.chmod(member.mode)


def cached_bundle(cache_dir: Path, name: str) -> bytes | None:
    """Bytes of a previously built bundle, if the cache still holds them."""
    path = cache_dir / f"spark-pulse-agent-{name}.tar.gz"
    if path.exists():
        return path.read_bytes()
    return None


def prune_cache(cache_dir: Path, keep: int = 3) -> int:
    """Keep the newest ``keep`` bundles; return how many were removed."""
    if not cache_dir.is_dir():
        return 0
    files = sorted(
        cache_dir.glob("spark-pulse-agent-*.tar.gz"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    removed = 0
    for path in files[keep:]:
        path.unlink(missing_ok=True)
        removed += 1
    return removed
