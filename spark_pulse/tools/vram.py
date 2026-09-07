"""Will this model fit, at this context length, on this node?

A deploy that asks for more memory than the machine has does not fail at
launch. It pulls an image, starts a container, loads weights for several
minutes and then dies in the CUDA allocator — and the operator reads a
traceback about a failed allocation rather than "48 GB of KV cache does not
fit in 30 GB". This module is the arithmetic that lets the pre-flight say so
first.

**Weights come from the bytes on disk, not from a parameter count.** The usual
approach is ``params × bytes_per_element``, which needs a parameter count and a
dtype and gets both wrong on quantised checkpoints — an AWQ 70B is not 70e9
times two. Spark Pulse only ever estimates for models it already has, and the
files in the snapshot *are* the tensors that will be loaded: same dtype, same
quantisation, already summed by the hub-cache verifier that pre-flight runs on
each node. Reading the size is both simpler and more accurate than
reconstructing it.

**The KV cache is where the context length bites**, and it is the half an
operator cannot eyeball. It grows linearly with ``max_model_len`` and does not
appear in any file, so a 7B model that fits comfortably at 4k can be
impossible at 128k. Two layouts are supported because they are the two in use:

* the ordinary one — multi-head and grouped-query attention alike, where each
  token costs ``2 × layers × kv_heads × head_dim`` elements (K and V);
* multi-head latent attention (DeepSeek's MLA), which caches one compressed
  latent plus a rope slice per layer instead, and which the ordinary formula
  overestimates by an order of magnitude.

Everything here is arithmetic on numbers somebody else gathered. Nothing in
this module reads a file, opens a socket or knows what a node is.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "ModelShape",
    "Workload",
    "VramEstimate",
    "shape_from_config",
    "estimate",
    "KV_LAYOUT_DENSE",
    "KV_LAYOUT_MLA",
    "KV_LAYOUT_UNKNOWN",
]

GIB = 1024**3

#: Each token costs ``2 × layers × kv_heads × head_dim`` elements — K and V,
#: per layer. Multi-head and grouped-query attention differ only in how many
#: kv heads there are, which is why they share a layout.
KV_LAYOUT_DENSE = "dense"
#: Multi-head latent attention: one compressed latent plus a rope slice per
#: layer per token, and no separate V. DeepSeek-V2 and later.
KV_LAYOUT_MLA = "mla"
#: The config did not carry enough to say. An estimate is still made for the
#: weights; the KV half is reported as unknown rather than guessed.
KV_LAYOUT_UNKNOWN = "unknown"

#: Bytes per element, by the ``torch_dtype`` a config declares.
#:
#: Only the KV cache is sized from this. Weights are measured on disk, so a
#: dtype this table has never heard of costs an unknown KV estimate and not a
#: wrong weight one.
_DTYPE_BYTES: dict[str, float] = {
    "float32": 4.0,
    "float": 4.0,
    "fp32": 4.0,
    "bfloat16": 2.0,
    "bf16": 2.0,
    "float16": 2.0,
    "fp16": 2.0,
    "half": 2.0,
    "float8_e4m3fn": 1.0,
    "float8_e5m2": 1.0,
    "fp8": 1.0,
    "int8": 1.0,
    "uint8": 1.0,
}

#: What the runtime itself wants, on top of weights and cache.
#:
#: CUDA context, cuBLAS and NCCL workspaces, activations for a batch, the
#: engine's own Python. A fixed figure rather than a fraction: it does not
#: scale with the model, and pretending to know it more precisely than this
#: would be false confidence. Chosen to be the sort of number that makes the
#: check conservative — an estimate that says "fits" and then does not is the
#: only outcome worse than no estimate at all.
RUNTIME_OVERHEAD_BYTES = 2 * GIB


@dataclass(frozen=True)
class ModelShape:
    """What a model's ``config.json`` says about its attention.

    Every field is optional because every field is missing from some real
    checkpoint. What cannot be read is reported as unknown rather than
    defaulted to a number that would be quietly wrong.
    """

    #: Bytes the weights occupy on disk. The figure the pre-flight's hub-cache
    #: verifier already computes per node.
    weight_bytes: int | None = None
    num_layers: int | None = None
    num_kv_heads: int | None = None
    head_dim: int | None = None
    #: The dtype the KV cache is held in. Absent means the weights' dtype.
    kv_dtype: str | None = None
    #: MLA only.
    kv_lora_rank: int | None = None
    qk_rope_head_dim: int | None = None
    layout: str = KV_LAYOUT_UNKNOWN
    model_type: str | None = None

    def can_size_kv(self) -> bool:
        """Whether the KV cache can be computed rather than guessed."""
        if self.layout == KV_LAYOUT_DENSE:
            return bool(self.num_layers and self.num_kv_heads and self.head_dim)
        if self.layout == KV_LAYOUT_MLA:
            return bool(self.num_layers and self.kv_lora_rank)
        return False


@dataclass(frozen=True)
class Workload:
    """What the deploy is asking for."""

    max_model_len: int | None = None
    tensor_parallel: int = 1
    pipeline_parallel: int = 1
    #: Concurrent sequences the cache is sized for. vLLM allocates a pool
    #: rather than per-request, so this is what the pool is asked to hold.
    max_num_seqs: int = 1


@dataclass
class VramEstimate:
    """What it will take, and whether that is more than there is."""

    weight_bytes: int | None = None
    kv_bytes: int | None = None
    overhead_bytes: int = RUNTIME_OVERHEAD_BYTES
    #: Per GPU, after tensor and pipeline parallelism have divided the model.
    total_bytes: int | None = None
    #: What the node said it had free. None when nothing could say.
    available_bytes: int | None = None
    layout: str = KV_LAYOUT_UNKNOWN
    #: Why a figure is missing, in the operator's words. Never empty when
    #: something is None.
    unknowns: list[str] = field(default_factory=list)

    @property
    def known(self) -> bool:
        """Whether there is a total to compare against anything."""
        return self.total_bytes is not None

    @property
    def fits(self) -> bool | None:
        """True, False, or None when there is not enough to say.

        Three states on purpose. A check that collapses "will not fit" and "I
        could not tell" into one answer teaches operators to ignore it.
        """
        if self.total_bytes is None or self.available_bytes is None:
            return None
        return self.total_bytes <= self.available_bytes

    @property
    def headroom_bytes(self) -> int | None:
        if self.total_bytes is None or self.available_bytes is None:
            return None
        return self.available_bytes - self.total_bytes

    def to_dict(self) -> dict[str, Any]:
        return {
            "weight_bytes": self.weight_bytes,
            "kv_bytes": self.kv_bytes,
            "overhead_bytes": self.overhead_bytes,
            "total_bytes": self.total_bytes,
            "available_bytes": self.available_bytes,
            "headroom_bytes": self.headroom_bytes,
            "layout": self.layout,
            "fits": self.fits,
            "unknowns": list(self.unknowns),
        }


def _dtype_bytes(name: str | None) -> float | None:
    if not name:
        return None
    return _DTYPE_BYTES.get(str(name).strip().lower().removeprefix("torch."))


def _int(value: Any) -> int | None:
    """A positive int, or None. Configs carry strings, nulls and zeroes."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def shape_from_config(
    config: dict[str, Any] | None, *, weight_bytes: int | None = None
) -> ModelShape:
    """Read a HuggingFace ``config.json`` summary into a :class:`ModelShape`.

    The layout is decided by what the config *has*, not by a list of model
    names: ``kv_lora_rank`` is what makes a model MLA, and matching on
    ``model_type == "deepseek_v3"`` would need editing every time somebody
    publishes a new one.
    """
    if not isinstance(config, dict):
        return ModelShape(weight_bytes=weight_bytes)

    layers = _int(config.get("num_hidden_layers")) or _int(config.get("n_layer"))
    kv_lora_rank = _int(config.get("kv_lora_rank"))

    # Grouped-query attention names its own head count; multi-head attention
    # does not, and there the kv heads are the attention heads.
    heads = _int(config.get("num_attention_heads")) or _int(config.get("n_head"))
    kv_heads = _int(config.get("num_key_value_heads")) or heads

    head_dim = _int(config.get("head_dim"))
    if head_dim is None:
        hidden = _int(config.get("hidden_size")) or _int(config.get("n_embd"))
        if hidden and heads:
            head_dim = hidden // heads or None

    layout = KV_LAYOUT_UNKNOWN
    if kv_lora_rank:
        layout = KV_LAYOUT_MLA
    elif layers and kv_heads and head_dim:
        layout = KV_LAYOUT_DENSE

    return ModelShape(
        weight_bytes=weight_bytes,
        num_layers=layers,
        num_kv_heads=kv_heads,
        head_dim=head_dim,
        kv_dtype=config.get("kv_dtype") or config.get("torch_dtype"),
        kv_lora_rank=kv_lora_rank,
        qk_rope_head_dim=_int(config.get("qk_rope_head_dim")),
        layout=layout,
        model_type=config.get("model_type"),
    )


def _kv_bytes_per_token(shape: ModelShape, element: float) -> int | None:
    """One token's cache, across every layer."""
    if shape.layout == KV_LAYOUT_MLA:
        if not (shape.num_layers and shape.kv_lora_rank):
            return None
        # One compressed latent plus the rope slice, per layer. No separate V:
        # that is the whole point of the layout, and applying the dense
        # formula here overstates the cache by roughly an order of magnitude.
        per_layer = shape.kv_lora_rank + (shape.qk_rope_head_dim or 0)
        return int(shape.num_layers * per_layer * element)
    if shape.layout == KV_LAYOUT_DENSE:
        if not (shape.num_layers and shape.num_kv_heads and shape.head_dim):
            return None
        return int(2 * shape.num_layers * shape.num_kv_heads * shape.head_dim * element)
    return None


def estimate(
    shape: ModelShape,
    workload: Workload | None = None,
    *,
    available_bytes: int | None = None,
) -> VramEstimate:
    """What this model will take per GPU, and whether that is more than free.

    Parallelism divides both halves: tensor parallelism shards the weights and
    the cache across ranks, pipeline parallelism splits the layers. The
    overhead does not divide — every rank pays it.
    """
    workload = workload or Workload()
    unknowns: list[str] = []

    tp = max(int(workload.tensor_parallel or 1), 1)
    pp = max(int(workload.pipeline_parallel or 1), 1)
    ranks = tp * pp

    weight_bytes = shape.weight_bytes
    if weight_bytes is None:
        unknowns.append("the weights have not been measured on this node")

    kv_bytes: int | None = None
    element = _dtype_bytes(shape.kv_dtype)
    if not workload.max_model_len:
        unknowns.append(
            "no context length was resolved, so the KV cache cannot be sized"
        )
    elif not shape.can_size_kv():
        unknowns.append(
            "the model config does not describe its attention "
            "(layers, KV heads and head dimension), so the KV cache "
            "cannot be sized"
        )
    elif element is None:
        unknowns.append(
            f"the KV cache dtype {shape.kv_dtype!r} is not one this build "
            "knows the width of"
        )
    else:
        per_token = _kv_bytes_per_token(shape, element)
        if per_token is None:
            unknowns.append("the KV cache layout could not be sized")
        else:
            seqs = max(int(workload.max_num_seqs or 1), 1)
            kv_bytes = per_token * int(workload.max_model_len) * seqs

    total: int | None = None
    if weight_bytes is not None and kv_bytes is not None:
        total = (weight_bytes + kv_bytes) // ranks + RUNTIME_OVERHEAD_BYTES

    return VramEstimate(
        weight_bytes=weight_bytes,
        kv_bytes=kv_bytes,
        total_bytes=total,
        available_bytes=available_bytes,
        layout=shape.layout,
        unknowns=unknowns,
    )
