from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from sovereign_workbench.family_review import (
    build_family_proposals, decide_boundary, family_report, generate_research_tickets,
    import_evidence_return, propose_synthesis,
)
from sovereign_workbench.intake import scan_files
from sovereign_workbench.review_batches import (
    admit, connect, create_next, register_intake_root, store_candidates, store_relationships,
)


def _setup(tmp_path: Path):
    root = tmp_path / "archive"; root.mkdir()
    (root / "Solar Forge draft.txt").write_text(
        "The canonical policy prevents model confidence from granting execution authority. "
        "Independent evidence is required before the thermal claim is accepted.", encoding="utf-8")
    (root / "Solar Forge revised.txt").write_text(
        "The canonical policy does not permit model confidence to grant execution authority. "
        "The thermal claim requires primary experimental evidence before acceptance.", encoding="utf-8")
    database = connect(tmp_path / "state.db")
    register_intake_root(database, root, purpose="test", allowed_suffixes={".txt"})
    admit(database, root, scan_files(root)); batch = create_next(database)
    candidates = []
    summaries = [
        "The canonical policy prevents model confidence from granting execution authority. "
        "Independent evidence is required before the thermal claim is accepted.",
        "The canonical policy does not permit model confidence to grant execution authority. "
        "The thermal claim requires primary experimental evidence before acceptance.",
    ]
    for item, summary in zip(batch["items"], summaries, strict=True):
        candidates.append({"source_id": item["source_id"], "model_summary": summary,
                           "proposed_topic": "Governance", "proposed_maturity": "Research",
                           "proposed_authority": "No authority", "proposal_reason": "test",
                           "classification_confidence": .8, "classification_abstained": False,
                           "risk_flags": []})
    store_candidates(database, batch["batch_id"], candidates)
    return database


def test_family_flow_is_hash_bound_inert_and_preserves_originals(tmp_path: Path):
    with _setup(tmp_path) as database:
        result = build_family_proposals(database)
        assert result["families"] == 1 and result["members"] == 2
        family_id, family_hash = database.execute(
            "SELECT family_id,family_sha256 FROM archive_document_families"
        ).fetchone()
        report = family_report(database, family_id)
        assert len(report["members"]) == 2
        assert report["authority"] == "NONE"
        assert report["claim_matrix"]
        assert report["unique_claims"]
        assert isinstance(report["contradictory_claims"], list)
        assert {item["relationship_type"] for item in report["relationship_proposals"]} == {
            "POSSIBLE_REVISION_OF"
        }
        tickets = generate_research_tickets(database, family_id)
        assert tickets and all(ticket["authority"] == "NONE" for ticket in tickets)
        ticket = tickets[0]
        evidence = {"contract_version": "sovereign.workbench.family-evidence-return.v1",
                    "ticket_id": ticket["ticket_id"], "ticket_sha256": ticket["ticket_sha256"],
                    "findings": [{"claim": "unresolved"}], "citations": [],
                    "uncertainties": ["primary evidence missing"], "authority": "NONE"}
        encoded = json.dumps(evidence, sort_keys=True, separators=(",", ":"))
        evidence["return_sha256"] = hashlib.sha256(encoded.encode()).hexdigest()
        assert import_evidence_return(database, evidence).startswith("FER-")
        with pytest.raises(ValueError, match="approval"):
            propose_synthesis(database, family_id)
        decide_boundary(database, family_id, family_hash, "APPROVE", "Reviewer",
                        "These files form one review family")
        synthesis = propose_synthesis(database, family_id)
        assert synthesis["synthesis"]["originals_preserved"] is True
        assert synthesis["synthesis"]["execution_authorized"] is False
        assert database.execute("SELECT COUNT(*) FROM review_sources").fetchone()[0] == 2


def test_family_and_relationship_boundaries_require_exact_hash(tmp_path: Path):
    with _setup(tmp_path) as database:
        build_family_proposals(database)
        family_id, family_hash = database.execute(
            "SELECT family_id,family_sha256 FROM archive_document_families"
        ).fetchone()
        proposal_id, proposal_hash = database.execute(
            "SELECT proposal_id,proposal_sha256 FROM archive_family_relationship_proposals"
        ).fetchone()
        with pytest.raises(ValueError, match="hash mismatch"):
            decide_boundary(database, family_id, "0" * 64, "APPROVE", "Reviewer", "reason")
        assert decide_boundary(database, proposal_id, proposal_hash, "NEEDS_RESEARCH",
                               "Reviewer", "Version direction is unresolved").startswith("FBD-")
        assert decide_boundary(database, family_id, family_hash, "REJECT", "Reviewer",
                               "Filename collision; not one family").startswith("FBD-")
        with pytest.raises(Exception, match="immutable"):
            database.execute("UPDATE archive_family_boundary_decisions SET decision='APPROVE'")


def test_exact_hash_duplicates_are_the_only_deterministic_relationship(tmp_path: Path):
    root = tmp_path / "archive"; root.mkdir()
    (root / "Project draft.txt").write_text("same", encoding="utf-8")
    (root / "Project final.txt").write_text("same", encoding="utf-8")
    with connect(tmp_path / "state.db") as database:
        register_intake_root(database, root, purpose="test", allowed_suffixes={".txt"})
        admit(database, root, scan_files(root)); batch = create_next(database)
        store_candidates(database, batch["batch_id"], [{
            "source_id": item["source_id"], "model_summary": "A sufficiently long claim about governed archival evidence.",
            "proposed_topic": "Other", "proposed_maturity": "Research",
            "proposed_authority": "No authority", "proposal_reason": "test",
            "classification_confidence": .7, "classification_abstained": False, "risk_flags": []
        } for item in batch["items"]])
        build_family_proposals(database)
        assert database.execute(
            "SELECT relationship_type FROM archive_family_relationship_proposals"
        ).fetchone()[0] == "EXACT_DUPLICATE_OF"


def test_existing_relationship_evidence_connects_differently_named_family_members(tmp_path: Path):
    root = tmp_path / "archive"; root.mkdir()
    (root / "ANNEX GAMMA.txt").write_text(
        "This annex records the governed thermal control requirements for the pilot.", encoding="utf-8")
    (root / "addendum gamma.txt").write_text(
        "This addendum records revised safeguards and unresolved experimental evidence.", encoding="utf-8")
    with connect(tmp_path / "state.db") as database:
        register_intake_root(database, root, purpose="test", allowed_suffixes={".txt"})
        admit(database, root, scan_files(root)); batch = create_next(database)
        items = {item["relative_path"]: item for item in batch["items"]}
        store_candidates(database, batch["batch_id"], [{
            "source_id": item["source_id"], "model_summary": "Bounded candidate summary for testing.",
            "proposed_topic": "Governance", "proposed_maturity": "Research",
            "proposed_authority": "No authority", "proposal_reason": "test",
            "classification_confidence": .7, "classification_abstained": False, "risk_flags": []
        } for item in batch["items"]])
        store_relationships(database, batch["batch_id"], [{
            "source_id": items["addendum gamma.txt"]["source_id"],
            "relationship_type": "POSSIBLE_PARENT", "related_relative_path": "ANNEX GAMMA.txt",
            "related_sha256": items["ANNEX GAMMA.txt"]["source_sha256"], "confidence": 1.0,
            "evidence": "Existing filename relationship evidence",
        }])
        result = build_family_proposals(database)
        assert result["families"] == 1 and result["members"] == 2
