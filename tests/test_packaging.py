"""Bundled data files must be declared as package data.

Several modules resolve a file that ships *inside* the package
(``spark_pulse/config.yaml``, ``spark_pulse/registries.yaml``, …) and degrade
quietly when it is missing — no defaults, no registries, an empty OCI page.
From a source checkout those files are always there, so the failure only shows
up in an installed wheel. These tests pin both halves: the path the code
resolves must exist, and ``pyproject.toml`` must declare it as package data.
"""

import fnmatch
from pathlib import Path

import pytest

from spark_pulse.config import _CONFIG_PATH
from spark_pulse.tools.oci_registry import BUNDLED_REGISTRIES_CONFIG

tomllib = pytest.importorskip("tomllib")

PACKAGE_ROOT = Path(__file__).resolve().parent.parent / "spark_pulse"
PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"

#: Every file the running code loads from inside the package.
BUNDLED_FILES = [_CONFIG_PATH, BUNDLED_REGISTRIES_CONFIG]


def _package_data_patterns() -> list[str]:
    data = tomllib.loads(PYPROJECT.read_text())
    return data["tool"]["setuptools"]["package-data"]["spark_pulse"]


@pytest.mark.parametrize("path", BUNDLED_FILES, ids=lambda p: p.name)
def test_bundled_file_lives_inside_the_package(path):
    """The resolved path points at a real file under ``spark_pulse/``."""
    assert path.exists(), f"{path} does not exist"
    assert path.is_relative_to(PACKAGE_ROOT), f"{path} is outside {PACKAGE_ROOT}"


@pytest.mark.parametrize("path", BUNDLED_FILES, ids=lambda p: p.name)
def test_bundled_file_is_declared_as_package_data(path):
    """A wheel built from ``pyproject.toml`` actually carries the file."""
    relative = path.relative_to(PACKAGE_ROOT).as_posix()
    patterns = _package_data_patterns()
    assert any(
        fnmatch.fnmatch(relative, pattern) for pattern in patterns
    ), f"{relative} matches none of the package-data patterns {patterns}"
