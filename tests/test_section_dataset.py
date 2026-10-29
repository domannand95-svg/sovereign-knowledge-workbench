import json
from pathlib import Path

import pytest

from sovereign_workbench.intake import scan_files
from sovereign_workbench.review_batches import (admit, connect, create_next,
                                                  register_intake_root, store_candidates)
from sovereign_workbench.section_dataset import (build_section_dataset_candidates,
                                                   export_section_dataset_summary,
                                                   materialize_approved_section_split,
                                                   propose_family_safe_split, _category)
from sovereign_workbench.corpus_hardening import apply_contamination_exclusions
from sovereign_workbench.section_review import (build_section_proposals, decide_document_map,
                                                  document_exception_maps)


def test_candidates_require_reviewed_maps_and_preserve_no_authority(tmp_path: Path):
    root = tmp_path / "archive"; root.mkdir()
    (root / "policy.txt").write_text(
        "Authority policy and schema validation prevent unauthorized execution.", encoding="utf-8")
    with connect(tmp_path / "review.db") as database:
        register_intake_root(database, root, purpose="test", allowed_suffixes={".txt"})
        admit(database, root, scan_files(root)); batch = create_next(database)
        item = batch["items"][0]
        store_candidates(database, batch["batch_id"], [{"source_id": item["source_id"],
            "model_summary": "Governed policy", "proposed_topic": "Governance",
            "proposed_maturity": "Research", "proposed_authority": "No authority",
            "proposal_reason": "test", "classification_confidence": .8,
            "classification_abstained": False, "risk_flags": []}])
        build_section_proposals(database)
        assert build_section_dataset_candidates(database)["candidate_count"] == 0
        document_map = document_exception_maps(database)[0]
        decide_document_map(database, item["source_id"], item["source_sha256"],
                            document_map["map_sha256"], "MAP_ACCEPTED", "Reviewer", "reviewed")
        audit = build_section_dataset_candidates(database)
        assert audit["candidate_count"] == 1
        assert audit["candidates"][0]["authority"] == "NONE"
        assert audit["candidates"][0]["allocation"] == "UNALLOCATED"
        assert audit["training_authorized"] is False


def test_split_proposal_never_crosses_family_or_authorizes_training():
    candidates = []
    categories = ["AUTHORITY_BOUNDARY", "AUTHORITY_ESCALATION_REJECTION",
                  "GENERAL_REASONING", "HALLUCINATION_HANDLING", "ORDINARY_CODING",
                  "OUT_OF_DOMAIN", "REGRESSION", "SCHEMA_CONTRACT_INTERPRETATION",
                  "SOVEREIGN_TERMINOLOGY", "STRUCTURED_OUTPUT"]
    for index in range(500):
        candidates.append({"candidate_id": f"C-{index}", "source_family_id": f"F-{index // 5}",
                           "behavior_category": categories[index % len(categories)],
                           "relative_path": "safe.txt",
                           "input": {"heading": "Safe", "bounded_excerpt": "Reviewed evidence"}})
    proposal = propose_family_safe_split(candidates)
    assert len(proposal["training_candidate_ids"]) == 300
    assert len(proposal["held_out_candidate_ids"]) == 100
    assert proposal["family_leakage"] == []
    assert proposal["ready_for_human_allocation_review"] is True
    assert proposal["approved"] is False
    assert proposal["training_authorized"] is False


def test_one_contaminated_section_quarantines_the_whole_source(tmp_path: Path):
    root = tmp_path / "archive"; root.mkdir()
    (root / "mixed.txt").write_text(
        "CLEAN POLICY:\nAuthority policy preserves deterministic capability boundaries.\n"
        "FRINGE CLOCK:\nA 432\\text{ Hz} harmonic baseline controls the reactor.\n",
        encoding="utf-8")
    with connect(tmp_path / "review.db") as database:
        register_intake_root(database, root, purpose="test", allowed_suffixes={".txt"})
        admit(database, root, scan_files(root)); batch = create_next(database)
        item = batch["items"][0]
        store_candidates(database, batch["batch_id"], [{"source_id": item["source_id"],
            "model_summary": "Mixed source", "proposed_topic": "Governance",
            "proposed_maturity": "Research", "proposed_authority": "No authority",
            "proposal_reason": "test", "classification_confidence": .8,
            "classification_abstained": False, "risk_flags": []}])
        build_section_proposals(database)
        document_map = document_exception_maps(database)[0]
        decide_document_map(database, item["source_id"], item["source_sha256"],
                            document_map["map_sha256"], "MAP_ACCEPTED", "Reviewer", "reviewed")
        apply_contamination_exclusions(database, reviewer="Reviewer")
        audit = build_section_dataset_candidates(database)
        assert audit["candidate_count"] == 0
        assert audit["omitted_counts"]["CONTAMINATED_SOURCE_QUARANTINE"] == 2


def test_strong_category_signals_do_not_require_synthetic_examples():
    assert _category("Research integrity", "Data honesty: no fabrication/falsification.")[0] == \
        "HALLUCINATION_HANDLING"
    assert _category("Claim contract", "pub struct ClaimAtom { claim_id: String }")[0] == \
        "SCHEMA_CONTRACT_INTERPRETATION"


def test_summary_is_compact_non_authoritative_and_non_overwriting(tmp_path: Path):
    categories = ["AUTHORITY_BOUNDARY", "AUTHORITY_ESCALATION_REJECTION",
                  "GENERAL_REASONING", "HALLUCINATION_HANDLING", "ORDINARY_CODING",
                  "OUT_OF_DOMAIN", "REGRESSION", "SCHEMA_CONTRACT_INTERPRETATION",
                  "SOVEREIGN_TERMINOLOGY", "STRUCTURED_OUTPUT"]
    candidates = [{"candidate_id": f"C-{index}", "source_family_id": f"F-{index // 5}",
                   "behavior_category": categories[index % 10], "relative_path": "safe.txt",
                   "input": {"heading": "Safe", "bounded_excerpt": "Reviewed evidence"}}
                  for index in range(500)]
    split = propose_family_safe_split(candidates)
    audit = {"audit_sha256": "a" * 64, "candidates": candidates, "split_proposal": split}
    audit_path = tmp_path / "audit.json"; audit_path.write_text(json.dumps(audit), encoding="utf-8")
    output = tmp_path / "summary.html"; export_section_dataset_summary(audit_path, output)
    text = output.read_text(encoding="utf-8")
    assert "training is not authorized" in text
    assert "I approve Audit-007" in text
    with pytest.raises(ValueError, match="already exists"):
        export_section_dataset_summary(audit_path, output)


def test_materialization_requires_exact_hash_and_remains_non_authoritative(tmp_path: Path):
    categories = ["AUTHORITY_BOUNDARY", "AUTHORITY_ESCALATION_REJECTION",
                  "GENERAL_REASONING", "HALLUCINATION_HANDLING", "ORDINARY_CODING",
                  "OUT_OF_DOMAIN", "REGRESSION", "SCHEMA_CONTRACT_INTERPRETATION",
                  "SOVEREIGN_TERMINOLOGY", "STRUCTURED_OUTPUT"]
    candidates = []
    for index in range(500):
        value = {"candidate_id": f"C-{index}", "source_id": f"S-{index // 2}",
                 "source_family_id": f"F-{index // 5}", "section_id": f"SEC-{index}",
                 "behavior_category": categories[index % 10], "relative_path": "safe.txt",
                 "input": {"heading": "Safe", "bounded_excerpt": "Reviewed evidence"},
                 "expected": {"document_map_decision": "MAP_ACCEPTED",
                              "proposed_topics": ["Governance"]}}
        candidates.append(value)
    audit = {"contract_version": "test", "authority": "NONE", "training_authorized": False,
             "candidate_count": 500, "candidates": candidates,
             "category_counts": {}, "omitted_counts": {}, "family_count": 100,
             "split_proposal": propose_family_safe_split(candidates)}
    encoded = json.dumps(audit, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    audit["audit_sha256"] = __import__("hashlib").sha256(encoded.encode()).hexdigest()
    path = tmp_path / "audit.json"; path.write_text(json.dumps(audit), encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        materialize_approved_section_split(path, tmp_path / "bad", expected_audit_sha256="0" * 64,
                                           reviewer="Reviewer")
    manifest = materialize_approved_section_split(
        path, tmp_path / "approved", expected_audit_sha256=audit["audit_sha256"], reviewer="Reviewer")
    assert manifest["training"]["examples"] == 300
    assert manifest["held_out"]["examples"] == 100
    assert manifest["training_authorized"] is False
