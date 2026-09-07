"""Will this model fit, at this context length, on this node?

The arithmetic exists to stop a deploy that cannot work from pulling an image,
starting a container, loading weights for several minutes and then dying in the
CUDA allocator. So the properties that matter are not "is the number pretty":
they are that it never claims to know what it does not, that it is
conservative when it does, and that the KV cache — the half that grows with
context and that nobody can eyeball — is right for both layouts in use.
"""

from __future__ import annotations

import pytest

from spark_pulse.tools import vram

GIB = 1024**3

#: Llama-3-8B's shape, as its own config.json carries it.
LLAMA_8B = {
    "model_type": "llama",
    "num_hidden_layers": 32,
    "num_attention_heads": 32,
    "num_key_value_heads": 8,
    "hidden_size": 4096,
    "torch_dtype": "bfloat16",
}

#: DeepSeek-V3's, which caches a latent rather than K and V.
DEEPSEEK = {
    "model_type": "deepseek_v3",
    "num_hidden_layers": 61,
    "num_attention_heads": 128,
    "kv_lora_rank": 512,
    "qk_rope_head_dim": 64,
    "hidden_size": 7168,
    "torch_dtype": "bfloat16",
}


# ── Reading a config ────────────────────────────────────────────────────────


class TestShapeFromConfig:
    def test_grouped_query_attention_keeps_its_own_head_count(self):
        shape = vram.shape_from_config(LLAMA_8B)

        assert shape.num_kv_heads == 8, "GQA has fewer KV heads than Q heads"
        assert shape.head_dim == 128, "4096 hidden / 32 heads"
        assert shape.layout == vram.KV_LAYOUT_DENSE

    def test_multi_head_attention_falls_back_to_the_attention_heads(self):
        """MHA does not name a KV head count because it has no separate one."""
        shape = vram.shape_from_config(
            {"num_hidden_layers": 12, "num_attention_heads": 12, "hidden_size": 768}
        )

        assert shape.num_kv_heads == 12

    def test_an_explicit_head_dim_wins_over_the_division(self):
        """Some models set head_dim independently of hidden/heads."""
        shape = vram.shape_from_config({**LLAMA_8B, "head_dim": 256})

        assert shape.head_dim == 256

    def test_the_layout_is_decided_by_the_fields_not_the_name(self):
        """`kv_lora_rank` is what makes a model MLA.

        Matching on ``model_type == "deepseek_v3"`` would need editing every
        time somebody publishes a new one.
        """
        shape = vram.shape_from_config({**DEEPSEEK, "model_type": "something_new"})

        assert shape.layout == vram.KV_LAYOUT_MLA
        assert shape.kv_lora_rank == 512

    def test_a_config_that_says_nothing_useful_is_unknown_not_guessed(self):
        shape = vram.shape_from_config({"model_type": "mystery"})

        assert shape.layout == vram.KV_LAYOUT_UNKNOWN
        assert shape.can_size_kv() is False

    @pytest.mark.parametrize("bad", [None, [], "not a dict", 7])
    def test_anything_that_is_not_a_config_is_survived(self, bad):
        shape = vram.shape_from_config(bad, weight_bytes=5)

        assert shape.weight_bytes == 5
        assert shape.layout == vram.KV_LAYOUT_UNKNOWN

    @pytest.mark.parametrize("zero", [0, -1, None, "", "twelve"])
    def test_a_field_that_is_not_a_positive_number_is_absent(self, zero):
        """Configs carry nulls, empty strings and zeroes. None of those is a
        layer count, and treating a 0 as one divides by it later."""
        shape = vram.shape_from_config({**LLAMA_8B, "num_hidden_layers": zero})

        assert shape.num_layers is None
        assert shape.can_size_kv() is False


# ── The KV cache ────────────────────────────────────────────────────────────


class TestTheKVCache:
    def test_dense_attention_caches_k_and_v_per_layer(self):
        """2 (K and V) × 32 layers × 8 heads × 128 dim × 2 bytes = 128 KiB/token."""
        shape = vram.shape_from_config(LLAMA_8B, weight_bytes=16 * GIB)

        estimate = vram.estimate(shape, vram.Workload(max_model_len=1))

        assert estimate.kv_bytes == 2 * 32 * 8 * 128 * 2

    def test_the_cache_grows_with_the_context_length(self):
        """The half an operator cannot eyeball, and the reason this exists."""
        shape = vram.shape_from_config(LLAMA_8B, weight_bytes=16 * GIB)

        short = vram.estimate(shape, vram.Workload(max_model_len=4096))
        long = vram.estimate(shape, vram.Workload(max_model_len=131072))

        assert long.kv_bytes == short.kv_bytes * 32

    def test_latent_attention_is_not_sized_with_the_dense_formula(self):
        """MLA caches one latent plus a rope slice per layer, and no V.

        Applying the dense formula to DeepSeek overstates the cache by about
        an order of magnitude, which would refuse deploys that fit fine.
        """
        mla = vram.shape_from_config(DEEPSEEK, weight_bytes=100 * GIB)
        workload = vram.Workload(max_model_len=8192)

        estimate = vram.estimate(mla, workload)

        assert estimate.layout == vram.KV_LAYOUT_MLA
        assert estimate.kv_bytes == 61 * (512 + 64) * 2 * 8192
        # What the dense formula would have said, for scale.
        dense_would_be = 2 * 61 * 128 * 56 * 2 * 8192
        assert estimate.kv_bytes < dense_would_be / 5

    def test_more_concurrent_sequences_need_more_cache(self):
        shape = vram.shape_from_config(LLAMA_8B, weight_bytes=16 * GIB)

        one = vram.estimate(shape, vram.Workload(max_model_len=4096, max_num_seqs=1))
        eight = vram.estimate(shape, vram.Workload(max_model_len=4096, max_num_seqs=8))

        assert eight.kv_bytes == one.kv_bytes * 8

    def test_an_fp8_cache_is_half_a_bfloat16_one(self):
        bf16 = vram.shape_from_config(LLAMA_8B, weight_bytes=GIB)
        fp8 = vram.shape_from_config({**LLAMA_8B, "kv_dtype": "fp8"}, weight_bytes=GIB)
        workload = vram.Workload(max_model_len=4096)

        assert vram.estimate(fp8, workload).kv_bytes == (
            vram.estimate(bf16, workload).kv_bytes // 2
        )


# ── Parallelism ─────────────────────────────────────────────────────────────


class TestParallelism:
    def test_tensor_parallelism_shards_the_model_across_ranks(self):
        shape = vram.shape_from_config(LLAMA_8B, weight_bytes=16 * GIB)
        workload = vram.Workload(max_model_len=4096)

        solo = vram.estimate(shape, workload)
        pair = vram.estimate(
            shape, vram.Workload(max_model_len=4096, tensor_parallel=2)
        )

        shardable = solo.total_bytes - vram.RUNTIME_OVERHEAD_BYTES
        assert pair.total_bytes == shardable // 2 + vram.RUNTIME_OVERHEAD_BYTES

    def test_every_rank_still_pays_the_runtime_overhead(self):
        """CUDA context and workspaces do not shard; only the model does."""
        shape = vram.shape_from_config(LLAMA_8B, weight_bytes=16 * GIB)

        many = vram.estimate(
            shape, vram.Workload(max_model_len=4096, tensor_parallel=8)
        )

        assert many.total_bytes > vram.RUNTIME_OVERHEAD_BYTES

    @pytest.mark.parametrize("degree", [0, -3, None])
    def test_a_nonsense_parallelism_degree_is_treated_as_one(self, degree):
        """Never a division by zero on a number that reached us from a form."""
        shape = vram.shape_from_config(LLAMA_8B, weight_bytes=GIB)

        estimate = vram.estimate(
            shape, vram.Workload(max_model_len=1024, tensor_parallel=degree)
        )

        assert estimate.total_bytes is not None


# ── Saying "I do not know" ──────────────────────────────────────────────────


class TestWhatItWillNotClaim:
    """Three states, not two.

    A check that collapses "will not fit" and "I could not tell" into one
    answer is a check operators learn to ignore.
    """

    def test_without_weights_there_is_no_total(self):
        shape = vram.shape_from_config(LLAMA_8B)

        estimate = vram.estimate(shape, vram.Workload(max_model_len=4096))

        assert estimate.total_bytes is None
        assert estimate.fits is None
        assert any("weights" in u for u in estimate.unknowns)

    def test_without_a_context_length_the_cache_is_unknown(self):
        shape = vram.shape_from_config(LLAMA_8B, weight_bytes=GIB)

        estimate = vram.estimate(shape, vram.Workload())

        assert estimate.kv_bytes is None
        assert estimate.total_bytes is None
        assert any("context length" in u for u in estimate.unknowns)

    def test_an_unreadable_config_says_so_rather_than_guessing(self):
        shape = vram.shape_from_config({"model_type": "mystery"}, weight_bytes=GIB)

        estimate = vram.estimate(shape, vram.Workload(max_model_len=4096))

        assert estimate.kv_bytes is None
        assert any("attention" in u for u in estimate.unknowns)

    def test_a_dtype_of_unknown_width_is_named(self):
        shape = vram.shape_from_config(
            {**LLAMA_8B, "kv_dtype": "float4_e2m1"}, weight_bytes=GIB
        )

        estimate = vram.estimate(shape, vram.Workload(max_model_len=4096))

        assert estimate.kv_bytes is None
        assert any("float4_e2m1" in u for u in estimate.unknowns)

    def test_with_nothing_free_reported_it_will_not_say_whether_it_fits(self):
        """A DGX Spark's nvidia-smi reports no GPU memory at all. Not knowing
        the budget is not the same as the model being too big."""
        shape = vram.shape_from_config(LLAMA_8B, weight_bytes=GIB)

        estimate = vram.estimate(shape, vram.Workload(max_model_len=4096))

        assert estimate.total_bytes is not None
        assert estimate.available_bytes is None
        assert estimate.fits is None
        assert estimate.headroom_bytes is None


# ── The verdict ─────────────────────────────────────────────────────────────


class TestFits:
    def test_a_model_that_fits_says_so_with_its_headroom(self):
        shape = vram.shape_from_config(LLAMA_8B, weight_bytes=16 * GIB)

        estimate = vram.estimate(
            shape, vram.Workload(max_model_len=4096), available_bytes=100 * GIB
        )

        assert estimate.fits is True
        assert estimate.headroom_bytes > 0

    def test_a_model_that_does_not_fit_says_so(self):
        shape = vram.shape_from_config(LLAMA_8B, weight_bytes=200 * GIB)

        estimate = vram.estimate(
            shape, vram.Workload(max_model_len=4096), available_bytes=100 * GIB
        )

        assert estimate.fits is False
        assert estimate.headroom_bytes < 0

    def test_the_context_length_alone_can_be_what_does_not_fit(self):
        """The failure this exists to catch: weights that fit comfortably, and
        a context length that does not."""
        shape = vram.shape_from_config(LLAMA_8B, weight_bytes=16 * GIB)
        free = 40 * GIB

        assert (
            vram.estimate(
                shape, vram.Workload(max_model_len=4096), available_bytes=free
            ).fits
            is True
        )
        assert (
            vram.estimate(
                shape,
                vram.Workload(max_model_len=131072, max_num_seqs=8),
                available_bytes=free,
            ).fits
            is False
        )

    def test_the_estimate_serialises_for_the_api(self):
        shape = vram.shape_from_config(LLAMA_8B, weight_bytes=16 * GIB)

        body = vram.estimate(
            shape, vram.Workload(max_model_len=4096), available_bytes=100 * GIB
        ).to_dict()

        assert body["fits"] is True
        assert body["layout"] == vram.KV_LAYOUT_DENSE
        assert body["headroom_bytes"] > 0
        assert body["unknowns"] == []
