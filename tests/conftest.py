"""Shared pytest setup.

Make sure the checkout the tests live in is what gets imported, even when a
`spark-pulse` editable install elsewhere in the environment points at a
different working tree.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture(autouse=True)
def isolate_imported_recipes(tmp_path, monkeypatch):
    """Keep recipe listing away from the developer's real ~/.config import dir."""
    from spark_pulse.tools import recipe_import

    monkeypatch.setattr(recipe_import, "IMPORTED_DIR", tmp_path / "_imported")
