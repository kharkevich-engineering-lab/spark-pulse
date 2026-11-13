"""Whether a recipe runs on the fleet this control plane has (#172).

The lab: an x86_64 workstation was offered every recipe, deployed one, waited
for its model to download, and then failed at the pull — the image had no
amd64 build. The node says its platform, the index says each image's, so the
answer is computed before anything moves.
"""

from __future__ import annotations

from unittest.mock import patch

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
#: A platform no engine image is published for, so the end-to-end tests do not
#: depend on what the engine index carries today: amd64 stopped being one the
#: day vLLM, SGLang and llama.cpp were published multi-arch.
UNSERVED = FleetNode("n-rv", "rv-board", "linux/riscv64")
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
        with patch.object(recipe_fit, "platforms_of", lambda _nodes: [UNSERVED]):
            response = client.post(
                "/api/deployments/plan", json={"recipe_id": recipe_id}
            )
        assert response.status_code == 400, response.text
        assert "no build for linux/riscv64 (rv-board)" in response.text


class TestTheApi:
    def test_every_listed_recipe_carries_a_fit(self):
        listed = TestClient(create_app()).get("/api/recipes").json()
        assert listed
        assert all(r["fit"]["verdict"] in (RUNS, UNKNOWN, WONT_RUN) for r in listed)

    def test_a_fleet_no_image_serves_is_told_which_recipes_do_not_run(self):
        with patch.object(recipe_fit, "fleet", lambda **_kw: [UNSERVED]):
            listed = TestClient(create_app()).get("/api/recipes").json()
        wont = [r for r in listed if r["fit"]["verdict"] == WONT_RUN]
        assert wont, "no engine is published for riscv64"
        assert any("riscv64" in r["fit"]["reason"] for r in wont)

    def test_one_recipe_carries_its_fit_too(self):
        client = TestClient(create_app())
        recipe_id = client.get("/api/recipes").json()[0]["id"]
        assert "fit" in client.get(f"/api/recipes/{recipe_id}").json()


# ── Memory (#173) ────────────────────────────────────────────────────────────

GB = 10**9
DESKTOP_12 = FleetNode("n-desk", "ark-desktop", "linux/amd64", 12 * 1024**3)
SPARK_120 = FleetNode("n-a", "spark-a", "linux/arm64", 120 * GB)
SPARK_B_120 = FleetNode("n-b", "spark-b", "linux/arm64", 120 * GB)


class TestTheMemoryFit:
    def test_a_model_bigger_than_the_card_will_not_run_and_says_by_how_much(self):
        found = fit(recipe("llama-cpp"), [DESKTOP_12], [MULTI], need_bytes=39 * GB)
        assert found.verdict == WONT_RUN
        assert found.reason == "needs ~39 GB, 13 GB on ark-desktop"
        # The platforms still said which engines run it.
        assert found.engines == {"llama-cpp": ["default"]}

    def test_within_ten_percent_is_tight(self):
        found = fit(recipe("llama-cpp"), [DESKTOP_12], [MULTI], need_bytes=12 * GB)
        assert found.verdict == recipe_fit.TIGHT
        assert "of 13 GB on ark-desktop" in found.reason

    def test_room_to_spare_runs(self):
        assert fit(recipe("llama-cpp"), [DESKTOP_12], [MULTI], 2 * GB).verdict == RUNS

    def test_a_need_nobody_could_size_leaves_the_platform_verdict(self):
        assert fit(recipe("llama-cpp"), [DESKTOP_12], [MULTI], None).verdict == RUNS

    def test_a_node_whose_memory_is_unknown_is_not_a_verdict(self):
        assert fit(recipe("llama-cpp"), [DESKTOP], [MULTI], 900 * GB).verdict == RUNS

    def test_a_cluster_recipe_splits_its_need_over_the_nodes_it_takes(self):
        two = recipe("vllm", min_nodes=2, cluster_only=True)
        nodes = [SPARK_120, SPARK_B_120]
        assert fit(two, nodes, [ARM_ONLY], need_bytes=200 * GB).verdict == RUNS
        over = fit(two, nodes, [ARM_ONLY], need_bytes=300 * GB)
        assert over.verdict == WONT_RUN
        assert over.reason == "needs ~150 GB per node, 120 GB on spark-b"

    def test_the_largest_node_decides_a_solo_recipe(self):
        found = recipe_fit.memory_fit(39 * GB, [DESKTOP_12, SPARK_120])
        assert found is None


class TestTheMemoryIsGatheredInTheBackground:
    def test_the_first_listing_waits_on_nothing_and_the_next_reads_it(self):
        import threading

        recipe_fit._cache.clear()
        release = threading.Event()

        def slow() -> int:
            release.wait(5)
            return 7

        assert recipe_fit._cached(("test", "slow"), slow) is None
        release.set()
        for _ in range(100):
            if recipe_fit._cached(("test", "slow"), slow) == 7:
                break
            threading.Event().wait(0.01)
        assert recipe_fit._cached(("test", "slow"), slow) == 7
        recipe_fit._cache.clear()

    def test_a_failure_is_unknown_not_an_error(self):
        import threading

        recipe_fit._cache.clear()

        def boom() -> int:
            raise OSError("no hub")

        recipe_fit._cached(("test", "boom"), boom)
        for thread in threading.enumerate():
            if thread.name.startswith("recipe-fit-test"):
                thread.join(2)
        assert recipe_fit._cached(("test", "boom"), boom) is None
        recipe_fit._cache.clear()

    def test_a_listing_carries_the_memory_verdict(self, monkeypatch):
        # An arm64 node, so the bundled (arm64) engines pass on platform and
        # memory is what decides.
        small = FleetNode("n-s", "spark-small", "linux/arm64", 12 * 1024**3)
        monkeypatch.setattr(recipe_fit, "fleet", lambda **_kw: [small])
        monkeypatch.setattr(recipe_fit, "recipe_need", lambda _r: 900 * GB)
        listed = TestClient(create_app()).get("/api/recipes").json()
        assert any(
            r["fit"]["reason"] == "needs ~900 GB, 13 GB on spark-small" for r in listed
        )
