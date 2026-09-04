"""`spark-pulse recipes validate` CLI subcommand."""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from spark_pulse.cli import main

FIXTURES = Path(__file__).parent / "fixtures" / "recipes"


@pytest.fixture
def runner():
    return CliRunner()


class TestRecipesValidate:
    def test_validates_a_single_file(self, runner):
        result = runner.invoke(
            main, ["recipes", "validate", str(FIXTURES / "minimax-m2-awq.yaml")]
        )
        assert result.exit_code == 0
        assert "OK" in result.output
        assert "MiniMax-M2-AWQ" in result.output
        assert "1 valid, 0 invalid." in result.output

    def test_validates_a_directory_recursively(self, runner):
        result = runner.invoke(main, ["recipes", "validate", str(FIXTURES)])
        assert result.exit_code == 0
        assert "4 valid, 0 invalid." in result.output
        assert "(v2, Qwen3.5-122B-FP8)" in result.output

    def test_reports_failures_and_exits_non_zero(self, runner, tmp_path):
        bad = tmp_path / "bad.yaml"
        bad.write_text("name: only-a-name\n", encoding="utf-8")
        result = runner.invoke(main, ["recipes", "validate", str(bad)])
        assert result.exit_code == 1
        assert "FAIL" in result.output
        assert "container: field required" in result.output
        assert "0 valid, 1 invalid." in result.output

    def test_json_output(self, runner):
        result = runner.invoke(
            main,
            [
                "recipes",
                "validate",
                "--json",
                str(FIXTURES / "qwen3.5-122b-fp8-v2.yaml"),
            ],
        )
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload[0]["ok"] is True
        assert payload[0]["recipe_version"] == "2"

    def test_accepts_several_targets(self, runner, tmp_path):
        bad = tmp_path / "bad.yaml"
        bad.write_text("name: x\n", encoding="utf-8")
        result = runner.invoke(
            main,
            [
                "recipes",
                "validate",
                str(FIXTURES / "minimax-m2-awq.yaml"),
                str(bad),
            ],
        )
        assert result.exit_code == 1
        assert "1 valid, 1 invalid." in result.output

    def test_missing_target_is_a_usage_error(self, runner, tmp_path):
        result = runner.invoke(
            main, ["recipes", "validate", str(tmp_path / "nope.yaml")]
        )
        assert result.exit_code == 2
