"""Canonical Docker label namespace for Spark Pulse managed containers.

Every managed container carries ``spark-pulse.*`` labels (dotted, hyphenated).
Container labels are the source of truth for reconciliation, so producers
(``tools.docker``, ``tools.cluster``) and consumers (``tools.reconciliation``,
``routers.cluster``) must agree on the exact keys. Import them from here —
never spell a label out inline.
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


# ── Cluster ──────────────────────────────────────────────────────────────────

CLUSTER_LABEL = label("cluster")
ROLE_LABEL = label("role")
NODE_RANK_LABEL = label("node_rank")
HEAD_IP_LABEL = label("head_ip")
WORKER_IPS_LABEL = label("worker_ips")
RAY_LABEL = label("ray")
RAY_ENABLED_LABEL = label("ray_enabled")
RAY_READY_LABEL = label("ray_ready")

# Kept as an alias so reconciliation and the container name label agree.
CONTAINER_NAME_LABEL = NAME_LABEL

MANAGED_FILTER = f"{MANAGED_LABEL}=true"

__all__ = [
    "CLUSTER_LABEL",
    "CONTAINER_NAME_LABEL",
    "CREATED_AT_LABEL",
    "DEPLOYMENT_LABEL",
    "GENERATION_LABEL",
    "HEAD_IP_LABEL",
    "IMAGE_LABEL",
    "LABEL_PREFIX",
    "MANAGED_FILTER",
    "MANAGED_LABEL",
    "MEMORY_LIMIT_LABEL",
    "MODE_LABEL",
    "NAME_LABEL",
    "NODE_RANK_LABEL",
    "PRIVILEGED_LABEL",
    "RANK_LABEL",
    "RAY_ENABLED_LABEL",
    "RAY_LABEL",
    "RAY_READY_LABEL",
    "RECIPE_LABEL",
    "ROLE_LABEL",
    "SHM_SIZE_LABEL",
    "VERSION_LABEL",
    "WORKER_IPS_LABEL",
    "WORLD_SIZE_LABEL",
    "identity_labels",
    "label",
]
