"""The simulated recipe catalogue — the fixture the e2e suite deploys against.

``spark_pulse.mock.recipes`` is addressed by name rather than through
``spark_pulse.tools``: the package attribute is whichever module won the last
import (CLAUDE.md's import gotcha), and every assertion here is about the mock
specifically. The real module is imported the way ``test_no_checkout`` does it,
so the two can be compared side by side.

What matters about a mock is not that it returns *something* — it is that it
returns what its real twin would. So the listing, the detail and the rendered
command are compared against the real module for the same recipe files, and the
customization store is asserted to be the one the customization API writes to.
"""

from __future__ import annotations

import importlib
import types

import pytest

from spark_pulse.config import config
from spark_pulse.mock import recipes as mock_recipes
from spark_pulse.tools import custom_recipes, recipe_sources

real_recipes = importlib.import_module("spark_pulse.tools.recipes")

RECIPE_YAML = (
    "name: Qwen\n"
    "model: org/qwen\n"
    "container: vllm-node\n"
    "command: vllm serve org/qwen --port {port}\n"
    "defaults:\n"
    "  port: 9001\n"
    "  tensor_parallel: 2\n"
)


def _api(module: types.ModuleType) -> set[str]:
    """The public names a module owns: what it defines, plus its own constants.

    Names it merely imported (``Path``, ``re``, helper modules) are not part of
    anyone's contract, so they are left out.
    """
    return {
        name
        for name, value in vars(module).items()
        if not name.startswith("_")
        and not isinstance(value, types.ModuleType)
        and getattr(value, "__module__", module.__name__) == module.__name__
    }


def without_bundled(entries: list[dict]) -> list[dict]:
    """Drop the recipes shipped inside the package, which are always listed."""
    return [e for e in entries if e.get("source") != recipe_sources.SOURCE_BUNDLED]


@pytest.fixture(autouse=True)
def isolated_customizations(tmp_path, monkeypatch):
    """Keep the developer's own saved customizations out of every listing."""
    monkeypatch.setattr(
        custom_recipes, "_CUSTOM_PATH", tmp_path / "_custom-recipes.json"
    )


@pytest.fixture
def checkout(tmp_path):
    """A spark-vllm-docker checkout holding one recipe."""
    (tmp_path / "checkout" / "recipes").mkdir(parents=True)
    (tmp_path / "checkout" / "recipes" / "qwen.yaml").write_text(
        RECIPE_YAML, encoding="utf-8"
    )
    return tmp_path / "checkout"


@pytest.fixture
def no_sources(monkeypatch):
    """Every recipe source empty — not even the bundled ones."""
    monkeypatch.setattr(recipe_sources, "iter_recipe_payloads", lambda _path: [])


class TestContract:
    """The mock must offer its real twin's API and invent nothing of its own.

    The equivalent drift in ``mock/launch_script.py`` left every
    ``/api/launch-script`` endpoint answering 500 in simulation.
    """

    def test_the_mock_offers_every_name_the_real_module_does(self):
        missing = {n for n in _api(real_recipes) if not hasattr(mock_recipes, n)}
        assert missing == set()

    def test_the_mock_invents_no_api_of_its_own(self):
        assert _api(mock_recipes) <= _api(real_recipes)

    def test_customizations_come_from_the_store_the_api_writes_to(self):
        # The customization router writes through the real module in either
        # mode; a simulated listing reading a store of its own would report
        # is_customized=False for a recipe the operator just customized.
        assert mock_recipes.custom_recipes is custom_recipes


class TestListRecipes:
    def test_the_listing_matches_the_real_module_for_the_same_checkout(self, checkout):
        assert mock_recipes.list_recipes(
            spark_path=checkout
        ) == real_recipes.list_recipes(spark_path=checkout)

    def test_a_recipe_is_listed_with_its_parsed_fields(self, checkout):
        entry = without_bundled(mock_recipes.list_recipes(spark_path=checkout))

        assert len(entry) == 1
        assert entry[0]["id"] == "qwen"
        assert entry[0]["name"] == "Qwen"
        assert entry[0]["source"] == recipe_sources.SOURCE_UPSTREAM
        assert entry[0]["defaults"]["port"] == 9001
        assert entry[0]["is_customized"] is False

    def test_a_customized_recipe_is_flagged_in_the_listing(self, checkout):
        custom_recipes.save_customization("qwen", {"defaults": {"port": 9999}})

        entry = without_bundled(mock_recipes.list_recipes(spark_path=checkout))[0]

        assert entry["is_customized"] is True

    def test_it_reads_the_configured_checkout_when_no_path_is_given(
        self, checkout, monkeypatch
    ):
        monkeypatch.setattr(
            type(config), "spark_vllm_path", property(lambda self: str(checkout))
        )

        ids = [e["id"] for e in without_bundled(mock_recipes.list_recipes())]

        assert ids == ["qwen"]

    def test_the_canned_catalogue_stands_in_when_there_is_no_source(
        self, tmp_path, no_sources
    ):
        entries = mock_recipes.list_recipes(spark_path=tmp_path / "nowhere")

        assert [e["name"] for e in entries] == [
            r["name"] for r in mock_recipes._RECIPES
        ]
        for entry in entries:
            assert entry["id"] == entry["name"]
            assert entry["params"] == entry["defaults"]
            assert entry["source"] == recipe_sources.SOURCE_UPSTREAM
            assert entry["is_customized"] is False
            assert {s["engine"] for s in entry["engine_support"]} == {"vllm", "sglang"}

    def test_an_empty_checkout_lists_only_what_is_on_disk(self, tmp_path, no_sources):
        # A checkout that exists but holds no recipes is not "no source at all":
        # the operator's empty catalogue must not be papered over with demo data.
        (tmp_path / "recipes").mkdir()

        assert mock_recipes.list_recipes(spark_path=tmp_path) == []


class TestGetRecipe:
    def test_the_detail_matches_the_real_module_for_the_same_checkout(self, checkout):
        assert mock_recipes.get_recipe("qwen", spark_path=checkout) == (
            real_recipes.get_recipe("qwen", spark_path=checkout)
        )

    def test_it_reads_the_configured_checkout_when_no_path_is_given(
        self, checkout, monkeypatch
    ):
        monkeypatch.setattr(
            type(config), "spark_vllm_path", property(lambda self: str(checkout))
        )

        assert mock_recipes.get_recipe("qwen")["model"] == "org/qwen"

    def test_an_unknown_recipe_is_none(self, tmp_path):
        assert mock_recipes.get_recipe("does-not-exist", spark_path=tmp_path) is None

    def test_a_saved_customization_is_merged_into_the_detail(self, checkout):
        custom_recipes.save_customization(
            "qwen",
            {"command": "custom serve", "defaults": {"port": 9999}, "mods": ["mine"]},
        )

        detail = mock_recipes.get_recipe("qwen", spark_path=checkout)

        assert detail["command"] == "custom serve"
        assert detail["defaults"]["port"] == 9999
        assert detail["defaults"]["tensor_parallel"] == 2  # untouched by the override
        assert detail["params"] == detail["defaults"]
        assert detail["mods"] == ["mine"]

    def test_a_canned_recipe_resolves_by_name_when_nothing_is_on_disk(self, tmp_path):
        detail = mock_recipes.get_recipe("gpt-oss-120b", spark_path=tmp_path)

        assert detail is not None
        assert detail["id"] == "gpt-oss-120b"
        assert detail["model"] == "openai/gpt-oss-120b"
        assert detail["container"] == "vllm-node-mxfp4"
        assert detail["solo_only"] is True
        assert detail["recipe_version"] == "1"
        assert detail["params"] == detail["defaults"]

    def test_a_customization_reaches_a_canned_recipe_too(self, tmp_path):
        custom_recipes.save_customization(
            "gpt-oss-120b", {"defaults": {"port": 9999}, "container": "mine"}
        )

        detail = mock_recipes.get_recipe("gpt-oss-120b", spark_path=tmp_path)

        assert detail["defaults"]["port"] == 9999
        assert detail["container"] == "mine"


class TestBuildLaunchCommand:
    RECIPE = {
        "id": "qwen",
        "command": "vllm serve {model} --host {host} --port {port} -tp {tensor_parallel}",
    }
    PARAMS = {"host": "127.0.0.1", "port": 9100, "tensor_parallel": 4}

    def test_it_renders_the_command_production_would_render(self):
        assert mock_recipes.build_launch_command(
            self.RECIPE, self.PARAMS
        ) == recipe_sources.render_command(self.RECIPE, self.PARAMS)

    def test_placeholders_become_bare_values_not_repeated_flags(self):
        # The hand-rolled simulation this replaced substituted whole flags, so
        # "--host {host}" came out as "--host --host 127.0.0.1" and
        # "{tensor_parallel}" was never expanded at all.
        assert mock_recipes.build_launch_command(self.RECIPE, self.PARAMS) == (
            "vllm serve {model} --host 127.0.0.1 --port 9100 -tp 4"
        )
