"""Canonical Docker label namespace for Spark Pulse managed containers.

Every managed container carries ``spark-pulse.*`` labels (dotted, hyphenated).
Container labels are the source of truth for reconciliation, so the producer
(``tools.docker``) and the consumers (``tools.native_runtime``,
``tools.reconciliation``) must agree on the exact keys. Import them from here —
never spell a label out inline.

There was a second block here for the cluster orchestrator's own labels —
cluster name, role, node rank, head IP, Ray flags. That orchestrator is gone,
a cluster is a deployment of size N, and the labels went with it: keeping keys
nothing writes only invites something to start reading them again.
"""

from __future__ import annotations

LABEL_PREFIX = "spark-pulse."


def label(name: str) -> str:
    """Return the fully-qualified label key for ``name``."""
    return f"{LABEL_PREFIX}{name}"


# ── Common ───────────────────────────────────────────────────────────────────

MANAGED_LABEL = label("managed")
NAME_LABEL = label("name")
VERSION_LABEL = label("version")
CREATED_AT_LABEL = label("created_at")
IMAGE_LABEL = label("image")

# ── Deployment (solo) ────────────────────────────────────────────────────────

DEPLOYMENT_LABEL = label("deployment")
RECIPE_LABEL = label("recipe")
MODE_LABEL = label("mode")
MEMORY_LIMIT_LABEL = label("memory_limit_gb")
SHM_SIZE_LABEL = label("shm_size_gb")
PRIVILEGED_LABEL = label("privileged")

# ── Gang identity (native, per rank) ─────────────────────────────────────────
#
# A native deployment is a gang of ranks, so a container is identified by the
# deployment it belongs to, which attempt (generation) created it, which rank
# it is and how many ranks the gang has. Generation is what makes a container
# from an abandoned attempt unambiguously reapable: the name and the label both
# carry it, so "left over from the last try" is a fact rather than a guess.

GENERATION_LABEL = label("generation")
RANK_LABEL = label("rank")
WORLD_SIZE_LABEL = label("world_size")


def identity_labels(
    deployment: str, generation: int, rank: int, world_size: int
) -> dict[str, str]:
    """The labels that say which rank of which attempt a container is.

    Merged last by :meth:`ContainerMetadata.to_labels`, after everything the
    engine profile and the user's ``docker:`` block contribute, so nothing can
    shadow the identity reconciliation reads back.
    """
    return {
        DEPLOYMENT_LABEL: deployment,
        GENERATION_LABEL: str(generation),
        RANK_LABEL: str(rank),
        WORLD_SIZE_LABEL: str(world_size),
    }


# Kept as an alias so reconciliation and the container name label agree.
CONTAINER_NAME_LABEL = NAME_LABEL

MANAGED_FILTER = f"{MANAGED_LABEL}=true"

__all__ = [
    "CONTAINER_NAME_LABEL",
    "CREATED_AT_LABEL",
    "DEPLOYMENT_LABEL",
    "GENERATION_LABEL",
    "IMAGE_LABEL",
    "LABEL_PREFIX",
    "MANAGED_FILTER",
    "MANAGED_LABEL",
    "MEMORY_LIMIT_LABEL",
    "MODE_LABEL",
    "NAME_LABEL",
    "PRIVILEGED_LABEL",
    "RANK_LABEL",
    "RECIPE_LABEL",
    "SHM_SIZE_LABEL",
    "VERSION_LABEL",
    "WORLD_SIZE_LABEL",
    "identity_labels",
    "label",
]
