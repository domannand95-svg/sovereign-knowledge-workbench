from pathlib import Path

from sovereign_workbench.epistemic import assess
from sovereign_workbench.model import FileRecord
from sovereign_workbench.pipeline import analyze_workspace


def record(path: str, text: str) -> FileRecord:
    return FileRecord(path, "0" * 64, len(text), "text/plain", 0, text, "success")


def test_phase_v_remains_proposed_and_non_authoritative():
    result = assess(record("phase-v.md", "Proposed specification for Phase V"))
    assert result.category == "PROPOSED-SPEC"
    assert result.maturity == "proposed"
    assert result.authority_status == "non_authoritative_candidate"
    assert result.recommended_destination is None
    assert result.move_required == "not_determined"


def test_mixed_epistemic_signals_fail_to_review():
    result = assess(record("handover.md", "Handover of research evidence"))
    assert result.category == "UNKNOWN / NEEDS REVIEW"
    assert result.maturity == "ambiguous"


def test_directory_does_not_manufacture_maturity():
    result = assess(record("production/claim.txt", "Unrelated notes"))
    assert result.category == "UNKNOWN / NEEDS REVIEW"
    assert result.canonical_status == "UNRESOLVED"


def test_exact_duplicates_are_flagged_without_deletion(tmp_path: Path):
    (tmp_path / "a.md").write_text("Research hypothesis", encoding="utf-8")
    (tmp_path / "b.md").write_text("Research hypothesis", encoding="utf-8")
    report = analyze_workspace(tmp_path)
    assert report.artifact_assessments["a.md"].canonical_status == "UNRESOLVED"
    assert report.artifact_assessments["b.md"].canonical_status == "DUPLICATE"
    duplicate_proposal = next(p for p in report.proposals if p.action == "review_duplicate")
    assert duplicate_proposal.status == "proposed"
    assert duplicate_proposal.authority_required == "filesystem.delete"
