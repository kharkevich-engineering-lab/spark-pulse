"""What a recipe serves: the schema, the payload, the render and the refusals.

``serves`` is one field read in five places — the parser, the listing, the
vLLM renderer, every other engine's ``supports`` and the benchmark router —
and each test here pins one of them. The plan and the record carrying it are
in ``test_tools_native_runtime.py::TestServes``, beside the fixtures they need.
"""

from __future__ import annotations

from unittest.mock import patch

import jsonschema
import pytest
from fastapi.testclient import TestClient
from referencing import Registry, Resource

from spark_pulse import tools
from spark_pulse.app import create_app
from spark_pulse.engines import SglangEngine, Topology, VllmEngine
from spark_pulse.engines.registry import load_bundled_specs
from spark_pulse.tools import deploy_dispatch, recipe_sources
from spark_pulse.tools.recipe_schema import (
    DEFAULT_SERVES,
    SERVES,
    RecipeV1,
    RecipeV2,
    RecipeValidationError,
    load_schema,
    parse_recipe,
    schema_registry,
    serves_of,
)

V2_DOC = {"recipe_version": "2", "name": "Embed", "model": "Qwen/Qwen3-Embedding-4B"}
V1_DOC = {"name": "Chat", "container": "vllm-node", "command": "vllm serve x"}


@pytest.fixture(scope="module")
def validator():
    registry = Registry().with_resources(
        [(uri, Resource.from_contents(s)) for uri, s in schema_registry().items()]
    )
    return jsonschema.Draft7Validator(load_schema("recipe"), registry=registry)


# ── Schema and parser ────────────────────────────────────────────────────────


class TestSchema:
    def test_the_published_enum_is_the_parser_enum(self):
        """The JSON Schema and the pydantic model name the same five kinds."""
        published = load_schema("2")["properties"]["serves"]
        assert tuple(published["enum"]) == SERVES
        assert published["default"] == DEFAULT_SERVES == "chat"

    def test_absent_is_chat(self):
        recipe = parse_recipe(V2_DOC)
        assert isinstance(recipe, RecipeV2)
        assert recipe.serves == "chat"

    def test_an_empty_value_is_the_default_not_an_error(self):
        assert parse_recipe({**V2_DOC, "serves": None}).serves == "chat"

    @pytest.mark.parametrize("kind", SERVES)
    def test_every_kind_parses_and_validates(self, kind, validator):
        doc = {**V2_DOC, "serves": kind}
        assert parse_recipe(doc).serves == kind
        assert list(validator.iter_errors(doc)) == []

    def test_an_unknown_kind_is_refused_by_both(self, validator):
        doc = {**V2_DOC, "serves": "rerank"}
        with pytest.raises(RecipeValidationError) as exc:
            parse_recipe(doc)
        assert exc.value.errors[0].path == "serves"
        assert list(validator.iter_errors(doc))

    def test_v1_serves_chat(self):
        recipe = parse_recipe(V1_DOC)
        assert isinstance(recipe, RecipeV1)
        assert recipe.serves == "chat"
        assert parse_recipe({**V1_DOC, "serves": "chat"}).serves == "chat"

    def test_v1_cannot_claim_another_kind(self, validator):
        """A verbatim vLLM line nothing rewrites cannot promise embeddings."""
        doc = {**V1_DOC, "serves": "embedding"}
        with pytest.raises(RecipeValidationError):
            parse_recipe(doc)
        assert list(validator.iter_errors(doc))


class TestServesOf:
    @pytest.mark.parametrize(
        "source", [{}, {"serves": None}, {"serves": ""}, {"serves": 3}, None]
    )
    def test_anything_unsaid_reads_as_chat(self, source):
        assert serves_of(source) == "chat"

    def test_a_stored_kind_is_reported_verbatim(self):
        assert serves_of({"serves": "embedding"}) == "embedding"
        assert serves_of({"serves": "hologram"}) == "hologram"


class TestPayload:
    def test_a_v2_payload_and_its_summary_carry_it(self):
        payload = recipe_sources.to_payload(
            parse_recipe({**V2_DOC, "serves": "embedding"}), "custom-embed", "embed"
        )
        assert payload["serves"] == "embedding"
        assert recipe_sources.summarize(payload, False)["serves"] == "embedding"

    def test_a_v1_payload_says_chat(self):
        payload = recipe_sources.to_payload(parse_recipe(V1_DOC), "custom-c", "c")
        assert payload["serves"] == "chat"

    def test_the_engine_table_says_why_sglang_cannot(self):
        payload = recipe_sources.to_payload(
            parse_recipe(
                {
                    **V2_DOC,
                    "serves": "embedding",
                    "engines": {"vllm": {}, "sglang": {}},
                }
            ),
            "custom-embed",
            "embed",
        )
        support = {e["engine"]: e for e in payload["engine_support"]}
        assert support["vllm"]["supported"] is True
        assert support["sglang"]["supported"] is False
        assert "serves embedding" in support["sglang"]["reason"]


# ── Engines ──────────────────────────────────────────────────────────────────


def _engine(cls, name):
    return cls(next(s for s in load_bundled_specs() if s.engine == name))


EMBED = {
    "id": "embed",
    "model": "Qwen/Qwen3-Embedding-4B",
    "recipe_version": "2",
    "serves": "embedding",
    "params": {"port": 8000},
}


class TestVllmRunner:
    @pytest.fixture
    def vllm(self):
        return _engine(VllmEngine, "vllm")

    def test_embedding_renders_the_pooling_runner(self, vllm):
        command = vllm.render(EMBED, topology=Topology.solo()).command
        assert command.startswith(
            "vllm serve Qwen/Qwen3-Embedding-4B --port 8000 --runner pooling --nnodes 1"
        )
        assert command.count("--runner") == 1

    def test_chat_renders_no_runner(self, vllm):
        recipe = {**EMBED, "serves": "chat"}
        assert "--runner" not in vllm.render(recipe).command
        assert "--runner" not in vllm.render({**EMBED, "serves": None}).command

    @pytest.mark.parametrize("args", ["--runner pooling", "--runner=pooling"])
    def test_a_runner_in_the_recipe_args_is_left_alone(self, vllm, args):
        recipe = {**EMBED, "engines": {"vllm": {"args": f"{args} --trust-remote-code"}}}
        command = vllm.render(recipe).command
        assert command.count("--runner") == 1
        assert args in command

    def test_a_runner_in_the_extra_args_is_left_alone(self, vllm):
        command = vllm.render(EMBED, extra_args=["--runner", "pooling"]).command
        assert command.count("--runner") == 1
        assert command.endswith("--runner pooling")

    def test_a_flag_that_merely_starts_with_runner_does_not_count(self, vllm):
        recipe = {**EMBED, "engines": {"vllm": {"args": "--runner-x 1"}}}
        assert "--runner pooling" in vllm.render(recipe).command

    def test_vllm_claims_chat_and_embedding_only(self, vllm):
        assert vllm.supports(EMBED) == (True, "")
        ok, reason = vllm.supports({**EMBED, "serves": "image"})
        assert ok is False
        assert "serves image" in reason


class TestOtherEngines:
    def test_sglang_refuses_an_embedding_recipe(self):
        sglang = _engine(SglangEngine, "sglang")
        ok, reason = sglang.supports(EMBED)
        assert ok is False
        assert reason == "recipe serves embedding, and sglang here serves only chat"

    def test_sglang_still_serves_chat(self):
        sglang = _engine(SglangEngine, "sglang")
        assert sglang.supports({**EMBED, "serves": "chat"}) == (True, "")


# ── Records on the way out ───────────────────────────────────────────────────


class TestDispatch:
    def test_a_record_without_the_field_is_served_as_chat(self):
        old = {"id": "a", "created_at": "1"}
        new = {"id": "b", "created_at": "2", "serves": "embedding"}
        with patch.object(
            tools.native_runtime, "list_deployments", return_value=[old, new]
        ):
            listed = deploy_dispatch.list_deployments()
        assert [d["serves"] for d in listed] == ["chat", "embedding"]
        # Filled on the way out, not written back into what was stored.
        assert "serves" not in old

    def test_one_deployment_says_it_too(self):
        with (
            patch.object(tools.deployment_records, "get", return_value={"id": "a"}),
            patch.object(tools.native_runtime, "status", return_value={"id": "a"}),
        ):
            assert deploy_dispatch.get_deployment("a")["serves"] == "chat"

    def test_a_record_that_vanished_mid_read_is_still_none(self):
        with (
            patch.object(tools.deployment_records, "get", return_value={"id": "a"}),
            patch.object(tools.native_runtime, "status", return_value=None),
        ):
            assert deploy_dispatch.get_deployment("a") is None


# ── Benchmarks are chat-only ─────────────────────────────────────────────────


class TestBenchmarkRefusal:
    @pytest.fixture
    def client(self):
        return TestClient(create_app(), raise_server_exceptions=False)

    def _post(self, client, record):
        with (
            patch.object(tools.deployment_records, "get", return_value=record),
            patch.object(
                tools.benchmarking,
                "create_benchmark",
                return_value={"benchmark_id": "b1", "status": "running"},
            ) as create,
            patch.object(tools.benchmarking, "execute_benchmark"),
        ):
            resp = client.post("/api/benchmarks", json={"deployment_id": "dep-1"})
        return resp, create

    def test_an_embedding_run_is_refused_with_a_reason(self, client):
        resp, create = self._post(client, {"id": "dep-1", "serves": "embedding"})
        assert resp.status_code == 409
        assert "serves embedding" in resp.json()["detail"]
        create.assert_not_called()

    def test_a_chat_run_is_benchmarked(self, client):
        resp, create = self._post(client, {"id": "dep-1", "serves": "chat"})
        assert resp.status_code == 200
        create.assert_called_once()

    def test_a_record_from_before_the_field_is_chat(self, client):
        resp, _ = self._post(client, {"id": "dep-1"})
        assert resp.status_code == 200

    def test_an_unknown_run_is_left_to_the_benchmark(self, client):
        resp, create = self._post(client, None)
        assert resp.status_code == 200
        create.assert_called_once()
