"""Running one command on the node it arrived at.

This is where the plan's real prize is collected. Every operation here is a
single call into ``DockerService`` — the same class, with the same defaults,
on every node in the cluster, control node included. There is no per-node
branch, so there is nowhere for a per-node divergence to live. The thirty
semantic divergences ``docs/transport-reexamined.md`` counted between two
``NodeService`` implementations were possible because there were two
implementations; here there is one, and the executor's whole job is to not
become a second one.

Two consequences of that, both deliberate:

* The executor never interprets a result. It hands ``DockerService``'s return
  value to the codec and sends it. It does not normalise a status, does not
  translate an exception into a value, and does not decide that a missing
  container "counts as" stopped.
* Every exception becomes a :class:`~spark_pulse.agent.agent_pb2.CommandFailure`
  in the reply, never a gRPC error. The node was reachable and the outcome is
  definite; that is exactly what a failure payload says and exactly what a
  transport status cannot.
"""

from __future__ import annotations

import gzip
import io
import logging
import os
import tarfile
import tempfile
from pathlib import Path
from typing import Any, Callable

from spark_pulse.agent import agent_pb2 as pb
from spark_pulse.agent import codec
from spark_pulse.agent.facts import collect_facts

logger = logging.getLogger(__name__)

__all__ = ["LocalExecutor", "StaleEpochError"]


class StaleEpochError(RuntimeError):
    """A command carrying an epoch older than one already seen.

    Fencing happens at the resource, not at a leader election (§3.3): the
    agent that owns the Docker daemon is the thing that refuses, so a command
    issued by a control plane that has since been replaced cannot act even if
    it is still in flight somewhere.
    """


class LocalExecutor:
    """Executes commands against this machine's Docker daemon.

    Constructed with a container service so that a test can pass
    ``MockDockerService`` and exercise every branch with no daemon, no
    container and no hardware — which is most of the point of the agent being
    a plain object with a stream bolted on rather than a process you have to
    deploy to observe.
    """

    def __init__(self, docker_service: Any | None = None):
        self._docker = docker_service
        self._epoch = 0

    @property
    def docker(self) -> Any:
        """The container service, built on first use.

        Lazy so importing the agent does not talk to a Docker daemon, and so a
        node whose daemon is down still starts, connects, and reports that its
        daemon is down rather than failing to appear at all.
        """
        if self._docker is None:
            from spark_pulse import tools

            self._docker = tools.docker.DockerService()
        return self._docker

    @property
    def docker_or_none(self) -> Any | None:
        """The container service if one has been built, without building one.

        Fact collection asks for this rather than for :attr:`docker`: a
        heartbeat must never be the thing that first connects to a Docker
        daemon, because then a daemon that is down stops the heartbeat and the
        node reports as unreachable instead of as having no daemon.
        """
        return self._docker

    @property
    def epoch(self) -> int:
        return self._epoch

    def note_epoch(self, epoch: int) -> None:
        """Record the highest controller epoch seen."""
        self._epoch = max(self._epoch, int(epoch))

    # ── Dispatch ─────────────────────────────────────────────────────────

    def execute(
        self,
        command: pb.Command,
        *,
        progress: Callable[[dict[str, Any]], None] | None = None,
        cancel: Callable[[], bool] | None = None,
    ) -> pb.CommandResult:
        """Run one command and return its outcome as payload.

        Blocking; the agent runs it on a worker thread. Never raises.
        """
        result = pb.CommandResult(command_id=command.command_id)
        op = command.WhichOneof("op")
        if op is None:
            result.failure.CopyFrom(
                pb.CommandFailure(type="ValueError", message="command carries no op")
            )
            return result
        if command.epoch and command.epoch < self._epoch:
            result.failure.CopyFrom(
                pb.CommandFailure(
                    type=StaleEpochError.__name__,
                    message=(
                        f"command epoch {command.epoch} is older than "
                        f"{self._epoch}; a newer control plane has taken over"
                    ),
                )
            )
            return result
        self.note_epoch(command.epoch)

        handler = getattr(self, f"_op_{op}", None)
        if handler is None:  # pragma: no cover — every oneof arm has a handler
            result.failure.CopyFrom(
                pb.CommandFailure(
                    type="NotImplementedError", message=f"unknown operation {op}"
                )
            )
            return result
        try:
            handler(getattr(command, op), result, progress=progress, cancel=cancel)
        except Exception as exc:
            logger.exception("command %s (%s) failed", command.command_id, op)
            result.failure.CopyFrom(codec.encode_failure(exc))
        return result

    # ── Operations ───────────────────────────────────────────────────────

    def _op_run_container(self, req: pb.RunContainer, out: pb.CommandResult, **_: Any):
        kwargs = codec.decode_run_container(req)
        info = self.docker.run_container(**kwargs)
        out.container.CopyFrom(
            pb.ContainerRef(found=True, container=codec.encode_container_info(info))
        )

    def _op_ensure_directories(
        self, req: pb.EnsureDirectories, out: pb.CommandResult, **_: Any
    ):
        failed = self.docker.ensure_directories(list(req.paths))
        out.strings.CopyFrom(pb.StringList(values=[str(p) for p in failed or []]))

    def _op_stop_container(
        self, req: pb.StopContainer, out: pb.CommandResult, **_: Any
    ):
        kwargs: dict[str, Any] = {}
        if req.HasField("timeout"):
            kwargs["timeout"] = req.timeout
        out.boolean.CopyFrom(
            pb.BoolValue(value=bool(self.docker.stop_container(req.name, **kwargs)))
        )

    def _op_get_container_status(
        self, req: pb.GetContainerStatus, out: pb.CommandResult, **_: Any
    ):
        out.status.CopyFrom(
            codec.encode_container_status(self.docker.get_container_status(req.name))
        )

    def _op_exec_in_container(
        self, req: pb.ExecInContainer, out: pb.CommandResult, **_: Any
    ):
        kwargs: dict[str, Any] = {}
        if req.HasField("detach"):
            kwargs["detach"] = req.detach
        if req.HasField("timeout"):
            kwargs["timeout"] = req.timeout
        result = self.docker.exec_in_container(
            req.container, codec.decode_cmd(req.command), **kwargs
        )
        out.exec.CopyFrom(codec.encode_exec_result(result))

    def _op_copy_to_container(
        self, req: pb.CopyToContainer, out: pb.CommandResult, **_: Any
    ):
        # The bytes are staged in a temporary file and handed to the *same*
        # copy_to_container the local path uses, rather than to a second copy
        # routine that would have to be kept in step with it.
        with tempfile.TemporaryDirectory(prefix="spark-pulse-copy-") as staging:
            name = os.path.basename(req.source_name) or "payload"
            local = Path(staging) / name
            local.write_bytes(req.content)
            if req.HasField("mode"):
                os.chmod(local, req.mode)
            kwargs = {"timeout": req.timeout} if req.HasField("timeout") else {}
            ok = self.docker.copy_to_container(
                req.container, str(local), req.remote_path, **kwargs
            )
        out.boolean.CopyFrom(pb.BoolValue(value=bool(ok)))

    def _op_copy_dir_to_container(
        self, req: pb.CopyDirToContainer, out: pb.CommandResult, **_: Any
    ):
        with tempfile.TemporaryDirectory(prefix="spark-pulse-copy-") as staging:
            name = os.path.basename(req.source_name) or "payload"
            root = Path(staging) / name
            root.mkdir(parents=True, exist_ok=True)
            with gzip.GzipFile(fileobj=io.BytesIO(req.tar_gz)) as raw:
                with tarfile.open(fileobj=raw, mode="r|") as archive:
                    # filter="data" refuses absolute paths, "..", symlinks out
                    # of the tree and device nodes. An archive arrives from the
                    # control plane over an authenticated channel, but an agent
                    # unpacking as root is not where we want to be relying on
                    # that.
                    _extract(archive, root)
            kwargs = {"timeout": req.timeout} if req.HasField("timeout") else {}
            ok = self.docker.copy_to_container(
                req.container, str(root), req.remote_path, **kwargs
            )
        out.boolean.CopyFrom(pb.BoolValue(value=bool(ok)))

    def _op_get_logs(self, req: pb.GetLogs, out: pb.CommandResult, **_: Any):
        kwargs = {"tail": req.tail} if req.HasField("tail") else {}
        out.text.CopyFrom(
            pb.StringValue(value=self.docker.get_logs(req.name, **kwargs))
        )

    def _op_list_managed_containers(
        self, req: pb.ListManagedContainers, out: pb.CommandResult, **_: Any
    ):
        labels = dict(req.labels) or None
        containers = self.docker.list_managed_containers(labels)
        out.containers.CopyFrom(
            pb.ContainerList(
                containers=[codec.encode_container_info(c) for c in containers]
            )
        )

    def _op_get_container_by_deployment(
        self, req: pb.GetContainerByDeployment, out: pb.CommandResult, **_: Any
    ):
        info = self.docker.get_container_by_deployment(req.deployment)
        if info is None:
            out.container.CopyFrom(pb.ContainerRef(found=False))
        else:
            out.container.CopyFrom(
                pb.ContainerRef(found=True, container=codec.encode_container_info(info))
            )

    def _op_get_container_by_recipe(
        self, req: pb.GetContainerByRecipe, out: pb.CommandResult, **_: Any
    ):
        containers = self.docker.get_container_by_recipe(req.recipe)
        out.containers.CopyFrom(
            pb.ContainerList(
                containers=[codec.encode_container_info(c) for c in containers or []]
            )
        )

    def _op_image_exists(self, req: pb.ImageExists, out: pb.CommandResult, **_: Any):
        out.boolean.CopyFrom(
            pb.BoolValue(value=bool(self.docker.image_exists(req.ref)))
        )

    def _op_image_info(self, req: pb.ImageInfo, out: pb.CommandResult, **_: Any):
        info = self.docker.image_info(req.ref)
        if info is None:
            out.image.CopyFrom(pb.ImageRef(found=False))
        else:
            out.image.CopyFrom(
                pb.ImageRef(found=True, image=codec.encode_image_info(info))
            )

    def _op_list_images(self, req: pb.ListImages, out: pb.CommandResult, **_: Any):
        images = self.docker.list_images() or []
        out.images.CopyFrom(
            pb.ImageList(images=[codec.encode_image_info(i) for i in images])
        )

    def _op_pull_image(
        self,
        req: pb.PullImage,
        out: pb.CommandResult,
        *,
        progress: Callable[[dict[str, Any]], None] | None = None,
        cancel: Callable[[], bool] | None = None,
        **_: Any,
    ):
        kwargs: dict[str, Any] = {}
        if req.HasField("interval"):
            kwargs["interval"] = req.interval
        if req.HasField("stall_timeout"):
            kwargs["stall_timeout"] = req.stall_timeout
        result = self.docker.pull_image(
            req.ref,
            progress if req.want_progress else None,
            cancel=cancel,
            **kwargs,
        )
        out.pull.CopyFrom(codec.encode_pull_outcome(result))

    def _op_remove_image(self, req: pb.RemoveImage, out: pb.CommandResult, **_: Any):
        kwargs = {"force": req.force} if req.HasField("force") else {}
        out.boolean.CopyFrom(
            pb.BoolValue(value=bool(self.docker.remove_image(req.ref, **kwargs)))
        )

    def _op_get_facts(self, req: pb.GetFacts, out: pb.CommandResult, **_: Any):
        out.facts.CopyFrom(collect_facts(self._docker))


def _extract(archive: tarfile.TarFile, root: Path) -> None:
    """Unpack an archive with the data filter where the runtime has one."""
    try:
        archive.extractall(root, filter="data")
    except TypeError:  # pragma: no cover — Python < 3.12 without the filter
        archive.extractall(root)
