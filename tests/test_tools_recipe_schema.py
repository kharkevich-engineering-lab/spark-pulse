"""Recipe schema: JSON Schema conformance, parsing, and v1 -> v2 conversion."""

from pathlib import Path

import jsonschema
import pytest
import yaml
from referencing import Registry, Resource

from spark_pulse.mock import recipes as mock_recipes
from spark_pulse.tools.recipe_schema import (
    EngineSpec,
    RecipeV1,
    RecipeV2,
    RecipeValidationError,
    detect_version,
    load_schema,
    parse_recipe,
    schema_registry,
    to_v2,
    validate_recipe_dir,
    validate_recipe_file,
)

FIXTURES = Path(__file__).parent / "fixtures" / "recipes"


@pytest.fixture(scope="module")
def validator():
    registry = Registry().with_resources(
        [
            (uri, Resource.from_contents(schema))
            for uri, schema in schema_registry().items()
        ]
    )
    return jsonschema.Draft7Validator(load_schema("recipe"), registry=registry)


def _fixture_docs():
    for path in sorted(FIXTURES.glob("*.yaml")):
        yield pytest.param(path, id=path.stem)


# ── JSON Schema conformance ──────────────────────────────────────────────────


class TestPublishedSchemas:
    def test_every_schema_is_a_valid_draft7_schema(self):
        for name in ("recipe", "1", "2"):
            jsonschema.Draft7Validator.check_schema(load_schema(name))

    def test_unknown_schema_name_raises(self):
        with pytest.raises(KeyError):
            load_schema("nope")

    @pytest.mark.parametrize("path", _fixture_docs())
    def test_fixture_recipes_match_the_combined_schema(self, path, validator):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert list(validator.iter_errors(doc)) == []

    @pytest.mark.parametrize("recipe", mock_recipes._RECIPES, ids=lambda r: r["name"])
    def test_mock_recipes_match_the_combined_schema(self, recipe, validator):
        assert list(validator.iter_errors(recipe)) == []

    @pytest.mark.parametrize("recipe", mock_recipes._RECIPES, ids=lambda r: r["name"])
    def test_mock_recipes_parse_strictly(self, recipe):
        parsed = parse_recipe(recipe)
        assert isinstance(parsed, RecipeV1)
        assert parsed.engines == ["vllm"]

    def test_v1_document_is_rejected_when_command_is_missing(self, validator):
        errors = list(validator.iter_errors({"name": "a", "container": "vllm-node"}))
        assert errors

    def test_v2_document_needs_a_model(self, validator):
        errors = list(validator.iter_errors({"recipe_version": "2", "name": "a"}))
        assert errors


# ── Version dispatch ─────────────────────────────────────────────────────────


class TestDetectVersion:
    def test_missing_version_means_v1(self):
        assert detect_version({"name": "x"}) == "1"

    def test_numeric_version_is_stringified(self):
        assert detect_version({"recipe_version": 2}) == "2"


class TestParseRecipe:
    def test_parses_a_mapping_as_v1(self):
        recipe = parse_recipe(
            {"name": "n", "container": "vllm-node", "command": "vllm serve m"}
        )
        assert isinstance(recipe, RecipeV1)
        assert recipe.recipe_version == "1"

    def test_parses_yaml_text(self):
        recipe = parse_recipe("name: n\ncontainer: vllm-node\ncommand: vllm serve m\n")
        assert isinstance(recipe, RecipeV1)
        assert recipe.container == "vllm-node"

    def test_parses_a_path(self):
        recipe = parse_recipe(FIXTURES / "minimax-m2-awq.yaml")
        assert isinstance(recipe, RecipeV1)
        assert recipe.name == "MiniMax-M2-AWQ"
        assert recipe.cluster_only is True

    def test_parses_a_v2_recipe(self):
        recipe = parse_recipe(FIXTURES / "qwen3.5-122b-fp8-v2.yaml")
        assert isinstance(recipe, RecipeV2)
        assert recipe.recipe_version == "2"
        assert recipe.engine == "vllm"
        assert recipe.engine_names() == ["vllm", "sglang"]
        assert recipe.constraints.min_nodes == 2
        assert recipe.params.tensor_parallel == 2
        assert recipe.engines["vllm"].mods == ["fix-qwen3.5-chat-template"]
        assert "instanttensor" in recipe.engines["vllm"].args_string()
        assert recipe.engines["sglang"].args_string().startswith("--tool-call-parser")

    def test_v2_params_keep_unknown_keys(self):
        recipe = parse_recipe(
            {
                "recipe_version": "2",
                "name": "n",
                "model": "org/m",
                "params": {"port": 8000, "kv_cache_dtype": "fp8"},
            }
        )
        assert recipe.params.as_dict() == {"port": 8000, "kv_cache_dtype": "fp8"}

    def test_lenient_mode_accepts_a_partial_v1_recipe(self):
        recipe = parse_recipe({"name": "n"}, strict=False)
        assert isinstance(recipe, RecipeV1)
        assert recipe.container == "vllm-node"
        assert recipe.command == ""

    def test_string_mods_are_wrapped_in_a_list(self):
        recipe = parse_recipe(
            {"name": "n", "container": "c", "command": "x", "mods": "one"},
        )
        assert recipe.mods == ["one"]

    def test_unsupported_type_raises_type_error(self):
        with pytest.raises(TypeError):
            parse_recipe(42)  # type: ignore[arg-type]


class TestValidationErrors:
    def test_missing_required_v1_fields_are_reported_with_paths(self):
        with pytest.raises(RecipeValidationError) as exc:
            parse_recipe({"name": "n"})
        paths = {e["path"] for e in exc.value.as_dicts()}
        assert paths == {"container", "command"}

    def test_missing_required_v2_fields_are_reported_with_paths(self):
        with pytest.raises(RecipeValidationError) as exc:
            parse_recipe({"recipe_version": "2", "name": "n"})
        assert [e["path"] for e in exc.value.as_dicts()] == ["model"]

    def test_unsupported_version_is_reported(self):
        with pytest.raises(RecipeValidationError) as exc:
            parse_recipe({"recipe_version": "9", "name": "n"})
        assert exc.value.errors[0].path == "recipe_version"
        assert "unsupported recipe version" in exc.value.errors[0].message

    def test_bad_field_type_reports_a_nested_path(self):
        with pytest.raises(RecipeValidationError) as exc:
            parse_recipe(
                {
                    "recipe_version": "2",
                    "name": "n",
                    "model": "m",
                    "params": {"tensor_parallel": 0},
                }
            )
        assert "params.tensor_parallel" in {e["path"] for e in exc.value.as_dicts()}

    def test_top_level_command_requires_an_engine(self):
        with pytest.raises(RecipeValidationError) as exc:
            parse_recipe(
                {
                    "recipe_version": "2",
                    "name": "n",
                    "model": "m",
                    "command": "vllm serve m",
                }
            )
        assert "pins the recipe to one engine" in str(exc.value)

    def test_declared_engine_must_be_described(self):
        with pytest.raises(RecipeValidationError) as exc:
            parse_recipe(
                {
                    "recipe_version": "2",
                    "name": "n",
                    "model": "m",
                    "engine": "sglang",
                    "engines": {"vllm": {}},
                }
            )
        assert "not described under 'engines'" in str(exc.value)

    def test_invalid_yaml_reports_the_source_file(self, tmp_path):
        bad = tmp_path / "bad.yaml"
        bad.write_text("name: [", encoding="utf-8")
        with pytest.raises(RecipeValidationError) as exc:
            parse_recipe(bad)
        assert str(bad) in str(exc.value)
        assert "invalid YAML" in exc.value.errors[0].message

    def test_empty_document_is_rejected(self, tmp_path):
        empty = tmp_path / "empty.yaml"
        empty.write_text("", encoding="utf-8")
        with pytest.raises(RecipeValidationError) as exc:
            parse_recipe(empty)
        assert "empty" in exc.value.errors[0].message

    def test_non_mapping_document_is_rejected(self):
        with pytest.raises(RecipeValidationError) as exc:
            parse_recipe("- a\n- b\n")
        assert "mapping" in exc.value.errors[0].message

    def test_missing_file_is_reported(self, tmp_path):
        with pytest.raises(RecipeValidationError) as exc:
            parse_recipe(tmp_path / "nope.yaml")
        assert "cannot read file" in exc.value.errors[0].message


# ── v1 -> v2 conversion ──────────────────────────────────────────────────────


class TestToV2:
    def test_golden_conversion(self):
        v1 = parse_recipe(FIXTURES / "nemotron-3-nano-nvfp4.yaml")
        v2 = to_v2(v1)

        assert v2.recipe_version == "2"
        assert v2.name == "Nemotron-3-Nano-NVFP4"
        assert v2.model == "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-NVFP4"
        assert v2.engine == "vllm"
        assert v2.engine_names() == ["vllm"]
        assert v2.constraints.solo_only is True
        assert v2.constraints.cluster_only is False
        assert v2.params.as_dict() == {
            "port": 8000,
            "host": "0.0.0.0",
            "tensor_parallel": 1,
            "gpu_memory_utilization": 0.7,
            "max_model_len": 262144,
        }
        spec = v2.engines["vllm"]
        assert spec.image == "vllm-node"
        assert spec.mods == ["mods/nemotron-nano"]
        assert spec.env == {}
        assert spec.command == v1.command
        assert "--moe-backend cutlass" in spec.command

    def test_conversion_carries_env_and_container(self):
        v1 = RecipeV1(
            name="n",
            container="vllm-node-b12x",
            command="vllm serve m",
            model="org/m",
            env={"VLLM_USE_V1": "1"},
            cluster_only=True,
        )
        v2 = to_v2(v1)
        assert v2.engines["vllm"] == EngineSpec(
            image="vllm-node-b12x",
            env={"VLLM_USE_V1": "1"},
            command="vllm serve m",
        )
        assert v2.constraints.cluster_only is True

    def test_converted_recipe_still_matches_the_v2_schema(self, validator):
        v2 = to_v2(parse_recipe(FIXTURES / "minimax-m2-awq.yaml"))
        doc = v2.model_dump(exclude_none=True)
        doc["params"] = v2.params.as_dict()
        assert list(validator.iter_errors(doc)) == []

    def test_unknown_defaults_survive_conversion(self):
        v1 = RecipeV1(
            name="n",
            container="c",
            command="x",
            model="m",
            defaults={"port": 9000, "kv_cache_dtype": "fp8"},
        )
        assert to_v2(v1).params.as_dict() == {"port": 9000, "kv_cache_dtype": "fp8"}


# ── File / directory helpers ─────────────────────────────────────────────────


class TestValidateHelpers:
    def test_validate_recipe_file_reports_success(self):
        result = validate_recipe_file(FIXTURES / "minimax-m2-awq.yaml")
        assert result["ok"] is True
        assert result["recipe_version"] == "1"
        assert result["name"] == "MiniMax-M2-AWQ"
        assert result["errors"] == []

    def test_validate_recipe_file_reports_failure_without_raising(self, tmp_path):
        bad = tmp_path / "bad.yaml"
        bad.write_text("name: only-a-name", encoding="utf-8")
        result = validate_recipe_file(bad)
        assert result["ok"] is False
        assert {e["path"] for e in result["errors"]} == {"container", "command"}

    def test_validate_recipe_dir_walks_subdirectories(self, tmp_path):
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "a.yaml").write_text(
            "name: a\ncontainer: c\ncommand: x\n", encoding="utf-8"
        )
        (tmp_path / "b.yml").write_text("name: b\n", encoding="utf-8")
        results = validate_recipe_dir(tmp_path)
        assert [r["ok"] for r in results] == [False, True]
