from __future__ import annotations

import json
from pathlib import Path

import pytest
from unittest.mock import patch

from sovereign_workbench.analysis import classify
from sovereign_workbench.model import FileRecord
from sovereign_workbench.pipeline import analyze_workspace
from sovereign_workbench.taxonomy import load_taxonomy


def write_taxonomy(path: Path, **changes) -> Path:
    value = {
        "contract_version": "sovereign.workbench.taxonomy.v1",
        "taxonomy_id": "private-project",
        "fallback_module": "manual_review",
        "modules": {"alpha": ["orchard"], "beta": ["reactor"]},
    }
    value.update(changes)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def record(text: str) -> FileRecord:
    return FileRecord("sample.txt", "0" * 64, len(text), "text/plain", 0, text, "success")


def test_custom_taxonomy_is_digest_bound_and_deterministic(tmp_path: Path):
    taxonomy = load_taxonomy(write_taxonomy(tmp_path / "taxonomy.json"))
    result = classify(record("Orchard evidence from the orchard"), taxonomy)
    assert result.module == "alpha"
    assert result.source == f"taxonomy:private-project:{taxonomy.sha256}"


def test_zero_score_and_ties_fail_to_manual_review(tmp_path: Path):
    taxonomy = load_taxonomy(write_taxonomy(tmp_path / "taxonomy.json"))
    assert classify(record("unmatched"), taxonomy).module == "manual_review"
    assert classify(record("orchard reactor"), taxonomy).module == "manual_review"


def test_taxonomy_terms_match_whole_words_not_substrings(tmp_path: Path):
    taxonomy = load_taxonomy(write_taxonomy(
        tmp_path / "taxonomy.json", modules={"ora": ["ora"], "ecology": ["restoration"]}
    ))
    assert classify(record("Ecological restoration"), taxonomy).module == "ecology"


@pytest.mark.parametrize("change", [
    {"contract_version": "future"},
    {"unexpected": True},
    {"modules": {}},
    {"modules": {"BAD ID": ["term"]}},
    {"modules": {"alpha": ["duplicate", "DUPLICATE"]}},
])
def test_invalid_contracts_fail_closed(tmp_path: Path, change):
    with pytest.raises(ValueError):
        load_taxonomy(write_taxonomy(tmp_path / "taxonomy.json", **change))


def test_report_records_taxonomy_provenance(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "sample.txt").write_text("reactor", encoding="utf-8")
    taxonomy_path = write_taxonomy(tmp_path / "taxonomy.json")
    taxonomy = load_taxonomy(taxonomy_path)
    report = analyze_workspace(source, taxonomy_path=taxonomy_path)
    assert report.taxonomy_contract == "sovereign.workbench.taxonomy.v1"
    assert report.taxonomy_sha256 == taxonomy.sha256
    assert report.classifications["sample.txt"].module == "beta"
    assert all(proposal.status == "proposed" for proposal in report.proposals)


def test_private_taxonomy_bounds_local_model_modules(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "sample.txt").write_text("reactor", encoding="utf-8")
    taxonomy_path = write_taxonomy(tmp_path / "taxonomy.json")
    with patch("sovereign_workbench.pipeline.classify_with_local_model") as model:
        model.side_effect = RuntimeError("captured")
        with pytest.raises(RuntimeError, match="captured"):
            analyze_workspace(source, taxonomy_path=taxonomy_path, use_local_model=True)
    assert model.call_args.kwargs["allowed_modules"] == ["alpha", "beta", "manual_review"]
