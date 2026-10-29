from pathlib import Path

import pytest

from sovereign_workbench.intake import scan_files
from sovereign_workbench.review_batches import (
    admit, connect, create_next, register_intake_root, store_candidates,
)
from sovereign_workbench.section_review import (
    build_section_proposals, decide_section_boundary, document_exception_maps,
    exception_queue, section_review_queue, split_sections,
)


def test_section_split_and_cross_family_proposals_preserve_one_original(tmp_path: Path):
    root = tmp_path / "archive"; root.mkdir()
    source = root / "Appendix K.txt"
    source.write_text(
        "SECTION 1: GOVERNANCE\nPolicy and quorum restrict execution authority and capability authorization.\n"
        "SECTION 2: MATERIAL RECOVERY\nCircular organics waste recovery supports biological metabolic recycling.\n"
        "SECTION 3: COMPUTE ENERGY\nNeuromorphic compute and photonic processor control plasma reactor power and thermal energy.",
        encoding="utf-8")
    with connect(tmp_path / "state.db") as database:
        register_intake_root(database, root, purpose="test", allowed_suffixes={".txt"})
        admit(database, root, scan_files(root)); batch = create_next(database)
        item = batch["items"][0]
        store_candidates(database, batch["batch_id"], [{
            "source_id": item["source_id"], "model_summary": "Multi-domain appendix",
            "proposed_topic": "Governance", "proposed_maturity": "Research",
            "proposed_authority": "No authority", "proposal_reason": "test",
            "classification_confidence": .6, "classification_abstained": False, "risk_flags": []}])
        result = build_section_proposals(database)
        assert result["sections"] == 3
        assert result["proposals"] >= 3
        topics = {row["proposed_topic"] for row in section_review_queue(database)}
        assert {"Governance", "Metabolism", "Compute", "Energy"} <= topics
        assert database.execute("SELECT COUNT(*) FROM review_sources").fetchone()[0] == 1
        assert source.exists()
        maps = document_exception_maps(database)
        assert len(maps) == 1 and maps[0]["review_required"] is True
        assert maps[0]["candidate"]["summary"] == "Multi-domain appendix"
        assert maps[0]["section_evidence"][0]["excerpt"]
        assert maps[0]["exceptions"]["cross_family"]
        assert maps[0]["exceptions"]["low_confidence"]["count"] == 0
        assert exception_queue(database) == maps


def test_section_decision_is_hash_bound_immutable_and_non_authoritative(tmp_path: Path):
    root = tmp_path / "archive"; root.mkdir()
    (root / "mixed.txt").write_text(
        "GOVERNANCE:\nPolicy quorum authority execution capability authorization.", encoding="utf-8")
    with connect(tmp_path / "state.db") as database:
        register_intake_root(database, root, purpose="test", allowed_suffixes={".txt"})
        admit(database, root, scan_files(root)); create_next(database)
        build_section_proposals(database); proposal = section_review_queue(database)[0]
        with pytest.raises(ValueError, match="hash mismatch"):
            decide_section_boundary(database, proposal["proposal_id"], "0" * 64,
                                    "APPROVE", "Reviewer", "correct section family")
        assert decide_section_boundary(database, proposal["proposal_id"], proposal["proposal_sha256"],
                                       "NEEDS_RESEARCH", "Reviewer",
                                       "Evidence is incomplete").startswith("SBD-")
        assert section_review_queue(database) == []
        with pytest.raises(Exception, match="immutable"):
            database.execute("UPDATE archive_section_boundary_decisions SET decision='APPROVE'")


def test_split_sections_falls_back_to_one_body_for_unheaded_text():
    assert split_sections("A plain paragraph without a heading.") == [
        ("Document opening", "A plain paragraph without a heading.")
    ]


def test_checksum_companion_is_preserved_but_not_sent_for_content_review(tmp_path: Path):
    root = tmp_path / "archive"; root.mkdir()
    source = root / "APPENDIX E hash.txt"
    source.write_text("APPENDIX E hash " + "a" * 64, encoding="utf-8")
    with connect(tmp_path / "state.db") as database:
        register_intake_root(database, root, purpose="test", allowed_suffixes={".txt"})
        admit(database, root, scan_files(root)); create_next(database)
        build_section_proposals(database)
        maps = document_exception_maps(database)
        assert maps[0]["artifact_role"] == "PROVENANCE_COMPANION"
        assert exception_queue(database) == []
        assert source.exists()


def test_fully_excluded_document_is_preserved_but_not_sent_for_map_review(tmp_path: Path):
    root = tmp_path / "archive"; root.mkdir()
    source = root / "theory.txt"
    source.write_text("THEORY:\nArbor Inversa cosmology.", encoding="utf-8")
    with connect(tmp_path / "state.db") as database:
        register_intake_root(database, root, purpose="test", allowed_suffixes={".txt"})
        admit(database, root, scan_files(root)); create_next(database); build_section_proposals(database)
        from sovereign_workbench.corpus_hardening import apply_contamination_exclusions
        apply_contamination_exclusions(database, reviewer="Reviewer")
        maps = document_exception_maps(database)
        assert maps[0]["artifact_role"] == "EXCLUDED_CONTENT"
        assert exception_queue(database) == []
    assert source.exists()
