"""Whether a recipe runs on the fleet this control plane has (#172).

The lab: an x86_64 workstation was offered every recipe, deployed one, waited
for its model to download, and then failed at the pull — the image had no
amd64 build. The node says its platform, the index says each image's, so the
answer is computed before anything moves.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from spark_pulse.app import create_app
from spark_pulse.tools import recipe_fit
from spark_pulse.tools.recipe_fit import (
    RUNS,
    UNKNOWN,
    WONT_RUN,
    FleetNode,
    Variant,
    fit,
    platform_refusal,
)

DESKTOP = FleetNode("n-desk", "ark-desktop", "linux/amd64")
SPARK_A = FleetNode("n-a", "spark-a", "linux/arm64")
SPARK_B = FleetNode("n-b", "spark-b", "linux/arm64")
OLD_AGENT = FleetNode("n-old", "spark-old", "")

ARM_ONLY = Variant("vllm", "default", ("linux/arm64",))
MULTI = Variant("llama-cpp", "default", ("linux/arm64", "linux/amd64"))


def recipe(*engines: str, **over) -> dict:
    support = [
        {"engine": e, "supported": True, "reason": "", "enabled": True} for e in engines
    ]
    return {"id": "r", "engine_support": support, **over}


class TestTheVerdict:
    def test_an_arm64_image_does_not_run_on_an_x86_desktop(self):
        found = fit(recipe("vllm"), [DESKTOP], [ARM_ONLY])
        assert found.verdict == WONT_RUN
        assert found.reason == "no amd64 image for vllm"

    def test_a_multi_arch_image_runs_on_both(self):
        assert fit(recipe("llama-cpp"), [DESKTOP], [MULTI]).verdict == RUNS
        assert fit(recipe("llama-cpp"), [SPARK_A], [MULTI]).verdict == RUNS

    def test_one_engine_that_runs_is_enough_and_is_named(self):
        found = fit(recipe("vllm", "llama-cpp"), [DESKTOP], [ARM_ONLY, MULTI])
        assert found.verdict == RUNS
        assert found.engines == {"llama-cpp": ["default"]}

    def test_a_cluster_recipe_needs_that_many_nodes_of_the_right_platform(self):
        two = recipe("vllm", min_nodes=2, cluster_only=True)
        assert fit(two, [SPARK_A, SPARK_B], [ARM_ONLY]).verdict == RUNS
        # Two nodes, but only one the image runs on.
        mixed = fit(two, [SPARK_A, DESKTOP], [ARM_ONLY])
        assert mixed.verdict == WONT_RUN

    def test_too_few_nodes_is_said_first(self):
        found = fit(recipe("vllm", cluster_only=True), [DESKTOP], [ARM_ONLY])
        assert found.reason == "needs 2 nodes, 1 available"

    def test_an_unknown_platform_is_not_a_mismatch(self):
        assert fit(recipe("vllm"), [OLD_AGENT], [ARM_ONLY]).verdict == UNKNOWN

    def test_an_engine_that_cannot_run_the_recipe_says_why(self):
        refused = {
            "id": "r",
            "engine_support": [
                {
                    "engine": "sglang",
                    "supported": False,
                    "reason": "serves embedding",
                    "enabled": True,
                }
            ],
        }
        found = fit(refused, [SPARK_A], [ARM_ONLY])
        assert found.verdict == WONT_RUN
        assert found.reason == "sglang: serves embedding"

    def test_an_unpublished_variant_does_not_count(self):
        unpublished = Variant("llama-cpp", "default", ("linux/amd64",), usable=False)
        found = fit(recipe("llama-cpp"), [DESKTOP], [unpublished])
        assert found.verdict == WONT_RUN
        assert "no published image" in found.reason


class TestThePlanRefusal:
    def test_a_known_mismatch_is_refused_with_the_node_named(self):
        said = platform_refusal("vllm", "default", ["linux/arm64"], [DESKTOP])
        assert "linux/amd64 (ark-desktop)" in said
        assert "published for linux/arm64" in said

    def test_an_unknown_node_is_not_refused(self):
        assert platform_refusal("vllm", "default", ["linux/arm64"], [OLD_AGENT]) == ""

    def test_a_match_is_not_refused(self):
        assert (
            platform_refusal("llama-cpp", "default", MULTI.arch, [DESKTOP, SPARK_A])
            == ""
        )

    def test_the_plan_refuses_an_image_built_for_another_platform(self):
        """Before the download and the pull, not after them."""
        client = TestClient(create_app())
        recipe_id = next(
            r["id"]
            for r in client.get("/api/recipes").json()
            if r.get("engine") == "vllm" and not r.get("cluster_only")
        )
        with patch.object(recipe_fit, "platforms_of", lambda _nodes: [DESKTOP]):
            response = client.post(
                "/api/deployments/plan", json={"recipe_id": recipe_id}
            )
        assert response.status_code == 400, response.text
        assert "no build for linux/amd64 (ark-desktop)" in response.text


class TestTheApi:
    def test_every_listed_recipe_carries_a_fit(self):
        listed = TestClient(create_app()).get("/api/recipes").json()
        assert listed
        assert all(r["fit"]["verdict"] in (RUNS, UNKNOWN, WONT_RUN) for r in listed)

    def test_an_x86_fleet_is_told_which_recipes_do_not_run(self):
        with patch.object(recipe_fit, "fleet", lambda: [DESKTOP]):
            listed = TestClient(create_app()).get("/api/recipes").json()
        wont = [r for r in listed if r["fit"]["verdict"] == WONT_RUN]
        assert wont, "the bundled engines are arm64-only"
        assert any("amd64" in r["fit"]["reason"] for r in wont)

    def test_one_recipe_carries_its_fit_too(self):
        client = TestClient(create_app())
        recipe_id = client.get("/api/recipes").json()[0]["id"]
        assert "fit" in client.get(f"/api/recipes/{recipe_id}").json()
