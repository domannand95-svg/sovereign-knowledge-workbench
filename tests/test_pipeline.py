import json
from pathlib import Path

from sovereign_workbench.intake import duplicate_groups, scan_files
from sovereign_workbench.package import build_review_package, canonical_json
from sovereign_workbench.pipeline import analyze_workspace


def test_scan_hashes_and_groups_exact_duplicates(tmp_path: Path):
    (tmp_path / "a.md").write_text("# Evidence\nResearch required: verify source.\n", encoding="utf-8")
    (tmp_path / "b.md").write_text("# Evidence\nResearch required: verify source.\n", encoding="utf-8")
    records = scan_files(tmp_path)
    assert len(records) == 2
    assert records[0].sha256 == records[1].sha256
    assert duplicate_groups(records) == [["a.md", "b.md"]]


def test_pipeline_proposes_but_does_not_mutate(tmp_path: Path):
    source = tmp_path / "brief.md"
    source.write_text("Government policy evidence. Citation needed for result.\n", encoding="utf-8")
    before = source.read_bytes()
    report = analyze_workspace(tmp_path)
    assert source.read_bytes() == before
    assert report.classifications["brief.md"].module in {"evidence", "governance"}
    assert report.research_queue[0]["status"] == "needs_review"
    assert all(proposal.status == "proposed" for proposal in report.proposals)


def test_sensitive_record_routes_to_privacy_review(tmp_path: Path):
    (tmp_path / "contact.txt").write_text("Contact person@example.org", encoding="utf-8")
    report = analyze_workspace(tmp_path)
    assert report.recipient_routes["contact.txt"] == ["privacy-review"]
    assert report.findings[0].kind == "email_address"


def test_review_package_has_stable_content_digest(tmp_path: Path):
    (tmp_path / "a.txt").write_text("Stable research evidence", encoding="utf-8")
    report = analyze_workspace(tmp_path)
    first = build_review_package(report)
    second = build_review_package(report)
    assert first["report_sha256"] == second["report_sha256"]
    assert json.loads(canonical_json(first))["external_dispatch"] == "not_authorized"
    assert "extracted_text" not in first["report"]["files"][0]


def test_scan_filters_suffix_and_count(tmp_path: Path):
    (tmp_path / "a.md").write_text("A", encoding="utf-8")
    (tmp_path / "b.md").write_text("B", encoding="utf-8")
    (tmp_path / "c.jpg").write_bytes(b"image")
    records = scan_files(tmp_path, include_suffixes={".md"}, max_files=1)
    assert [record.relative_path for record in records] == ["a.md"]


def test_domain_budget_is_not_automatically_finance(tmp_path: Path):
    (tmp_path / "paper.txt").write_text(
        "Research preprint about thermodynamic work budget and experimental evidence.",
        encoding="utf-8",
    )
    report = analyze_workspace(tmp_path)
    assert report.classifications["paper.txt"].module != "finance"


def test_source_code_does_not_create_research_gaps(tmp_path: Path):
    (tmp_path / "main.rs").write_text('eprintln!("unknown command");', encoding="utf-8")
    assert analyze_workspace(tmp_path).research_queue == []


def test_symlink_is_not_followed(tmp_path: Path):
    external = tmp_path.parent / "external-workbench-test.txt"
    external.write_text("outside", encoding="utf-8")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(external)
    except OSError:
        return
    assert scan_files(tmp_path) == []
