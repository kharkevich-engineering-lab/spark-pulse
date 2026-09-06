"""What survives a crash mid-write, and what an unusable config file means.

``atomic_json``'s module docstring states two rules for every persisted file
in this repository, and ``registries.yaml`` — the operator's list of OCI
registries — followed neither. It was written with a plain truncating
``open(path, "w")``, and a loader turned every failure into ``[]``.

YAML makes the first worse rather than better. A JSON file torn mid-write
fails to parse and is *detected*; a YAML file torn mid-write usually still
parses, into a plausible wrong value. The two observable outcomes of a crash
between the truncate and the flush were therefore a silently corrupted
registry URL, or silently zero registries.

The fix is the write, not the read: an unusable file still yields no
registries, because falling back to the bundled defaults would silently
substitute a public registry for an operator's private one, and pulling
recipes from somewhere nobody chose is worse than pulling none. What changed
on the read side is only that it now says so.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from spark_pulse.tools import oci_registry as oci
from spark_pulse.tools.atomic_json import write_text_atomic


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    """``registries.yaml`` in tmp_path, never the developer's own."""
    path = tmp_path / "registries.yaml"
    monkeypatch.setattr(oci, "REGISTRIES_CONFIG", path)
    return path


def _bundled_count() -> int:
    data = yaml.safe_load(oci.BUNDLED_REGISTRIES_CONFIG.read_text())
    return len(data["registries"])


# ── The write ───────────────────────────────────────────────────────────────


def test_a_saved_list_reads_back(config_path):
    oci._save_registries([{"name": "prod", "url": "ghcr.io/org/recipes"}])

    assert oci._load_registries() == [{"name": "prod", "url": "ghcr.io/org/recipes"}]


def test_the_write_leaves_no_temp_file_behind(config_path):
    oci._save_registries([{"name": "prod", "url": "ghcr.io/org/recipes"}])

    strays = [p.name for p in config_path.parent.iterdir() if p != config_path]
    assert strays == []


def test_a_failed_write_leaves_the_previous_file_intact(config_path, monkeypatch):
    """The property the rename buys: a reader sees the old file or the new one.

    A truncating write had no third state to offer — the old content was gone
    the moment the file was opened.
    """
    oci._save_registries([{"name": "prod", "url": "ghcr.io/org/recipes"}])

    def explode(*_args, **_kwargs):
        raise OSError("no space left on device")

    monkeypatch.setattr(yaml, "dump", explode)
    with pytest.raises(OSError):
        oci._save_registries([{"name": "other", "url": "ghcr.io/other"}])

    assert oci._load_registries() == [{"name": "prod", "url": "ghcr.io/org/recipes"}]


# ── The read ────────────────────────────────────────────────────────────────


def test_an_emptied_file_reports_none_loudly(config_path, caplog):
    """The exact state ``open(path, "w")`` leaves before the dump completes.

    It still yields no registries — falling back to the bundled defaults
    would silently substitute a public registry for an operator's private
    one — but it is no longer silent about it.
    """
    config_path.write_text("")

    with caplog.at_level("ERROR"):
        assert oci._load_registries() == []

    assert "no usable registry list" in caplog.text


def test_an_unparseable_file_reports_none(config_path):
    config_path.write_text("{{{ not yaml at all")

    assert oci._load_registries() == []


def test_a_file_of_the_wrong_shape_reports_none(config_path):
    config_path.write_text("registries: not-a-list")

    assert oci._load_registries() == []


def test_a_deliberately_empty_list_is_respected(config_path):
    """An operator who removed every registry meant it.

    This is why the fallback keys on "the file did not parse" rather than on
    "the list came back empty": the two are different answers and only one of
    them is a failure.
    """
    oci._save_registries([])

    assert oci._load_registries() == []


def test_a_missing_file_still_uses_the_bundled_defaults(config_path):
    assert not config_path.exists()

    assert len(oci._load_registries()) == _bundled_count()


# ── The shared helper ───────────────────────────────────────────────────────


def test_write_text_atomic_replaces_rather_than_truncates(tmp_path):
    target = tmp_path / "thing.yaml"
    target.write_text("old and complete\n")

    write_text_atomic(target, "new and complete\n")

    assert target.read_text() == "new and complete\n"
    assert [p.name for p in tmp_path.iterdir()] == ["thing.yaml"]


def test_write_text_atomic_honours_the_mode_it_is_given(tmp_path):
    target = tmp_path / "secret.yaml"

    write_text_atomic(target, "hunter2\n", mode=0o600)

    assert target.stat().st_mode & 0o077 == 0


def test_write_text_atomic_creates_the_directory(tmp_path):
    target: Path = tmp_path / "nested" / "deeper" / "thing.yaml"

    write_text_atomic(target, "content\n")

    assert target.read_text() == "content\n"
