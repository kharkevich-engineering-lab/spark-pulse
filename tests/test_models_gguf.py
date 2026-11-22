"""A GGUF repository's precision is in its file names (#lab).

A GGUF repo has no config.json, so the catalogue showed "—" for
Qwen2.5-0.5B-Instruct-GGUF, which holds nine quantisations. Each file's name
says its type, by the convention every GGUF publisher follows.
"""

from __future__ import annotations

import pytest

from spark_pulse.tools.models import _gguf_summary, gguf_quant


@pytest.mark.parametrize(
    "name, quant",
    [
        ("qwen2.5-0.5b-instruct-q8_0.gguf", "q8_0"),
        ("Qwen3-8B-Q4_K_M.gguf", "q4_k_m"),
        ("model-IQ2_XXS.gguf", "iq2_xxs"),
        ("model-f16.gguf", "f16"),
        ("model.BF16.gguf", "bf16"),
        # PrismML's ternary packings, which the prism engine reads.
        ("Ternary-Bonsai-2-27B-PQ2_0.gguf", "pq2_0"),
        ("Ternary-Bonsai-2-27B-PTQ1_0.gguf", "ptq1_0"),
        # A split file names its type before the part number.
        ("big-Q8_0-00001-of-00003.gguf", "q8_0"),
        ("no-quant-here.gguf", ""),
    ],
)
def test_the_quantisation_is_read_from_the_name(name, quant):
    assert gguf_quant(name) == quant


def test_a_snapshot_lists_each_gguf_once_with_its_size(tmp_path):
    (tmp_path / "m-q8_0.gguf").write_bytes(b"x" * 8)
    (tmp_path / "m-q4_k_m.gguf").write_bytes(b"x" * 4)
    for part in (1, 2):
        (tmp_path / f"big-Q6_K-0000{part}-of-00002.gguf").write_bytes(b"x" * 3)
    (tmp_path / "mmproj-f16.gguf").write_bytes(b"x")  # a projector, not a quant
    (tmp_path / "README.md").write_text("hi")

    found = {e["file"]: e for e in _gguf_summary(tmp_path)}

    assert set(found) == {"m-q8_0.gguf", "m-q4_k_m.gguf", "big-Q6_K.gguf"}
    assert found["big-Q6_K.gguf"] == {
        "file": "big-Q6_K.gguf",
        "quant": "q6_k",
        "size_bytes": 6,
    }
    assert found["m-q8_0.gguf"]["size_bytes"] == 8


def test_a_safetensors_snapshot_has_none(tmp_path):
    (tmp_path / "model.safetensors").write_bytes(b"x")
    assert _gguf_summary(tmp_path) == []
