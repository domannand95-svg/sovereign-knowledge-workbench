from pathlib import Path

import pytest

from sovereign_workbench.plugins import PluginRuntimeError, list_plugins, run_plugin


def test_installed_plugin_registry_is_visible():
    assert "privacy.detect" in {item["plugin_id"] for item in list_plugins()}


def test_plugin_execution_is_hash_bound_and_candidate_only(tmp_path: Path):
    path = tmp_path / "note.md"
    path.write_text("Contact person@example.org", encoding="utf-8")
    result = run_plugin("privacy.detect", path)
    assert result["authority"] == "none"
    assert result["status"] == "candidate"
    assert len(result["input_sha256"]) == 64


def test_plugin_suffix_mismatch_fails_closed(tmp_path: Path):
    path = tmp_path / "image.jpg"
    path.write_bytes(b"not-an-image")
    with pytest.raises(PluginRuntimeError):
        run_plugin("privacy.detect", path)
