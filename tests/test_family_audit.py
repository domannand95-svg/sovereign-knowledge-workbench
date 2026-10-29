from pathlib import Path

from sovereign_workbench.family_audit import audit_family_lineage, export_family_audit
from sovereign_workbench.intake import scan_files
from sovereign_workbench.review_batches import (admit, connect, create_next,
                                                  register_intake_root, store_candidates)


def _database(tmp_path: Path):
    root = tmp_path / "archive"; root.mkdir()
    (root / "design draft.txt").write_text("same governed evidence", encoding="utf-8")
    (root / "design final.txt").write_text("same governed evidence", encoding="utf-8")
    database = connect(tmp_path / "review.db")
    register_intake_root(database, root, purpose="test", allowed_suffixes={".txt"})
    admit(database, root, scan_files(root)); batch = create_next(database)
    store_candidates(database, batch["batch_id"], [{"source_id": item["source_id"],
        "model_summary": "Candidate only", "proposed_topic": "Governance",
        "proposed_maturity": "Research", "proposed_authority": "No authority",
        "proposal_reason": "test", "classification_confidence": .8,
        "classification_abstained": False, "risk_flags": []} for item in batch["items"]])
    return database, tmp_path / "review.db"


def test_audit_is_read_only_groups_duplicates_and_blocks_training(tmp_path: Path):
    database, state_db = _database(tmp_path)
    before = state_db.read_bytes()
    report = audit_family_lineage(database)
    assert report["source_count"] == 2
    assert report["family_count"] == 1
    assert report["exact_duplicates"][0]["count"] == 2
    assert report["dataset_audit"]["ready_for_fine_tuning"] is False
    assert report["training_authorized"] is False
    assert state_db.read_bytes() == before


def test_export_is_hash_bound_and_non_overwriting(tmp_path: Path):
    database, state_db = _database(tmp_path)
    output = tmp_path / "audit.json"
    report = export_family_audit(database, state_db, output)
    assert report["state_db_sha256"]
    assert report["audit_sha256"]
    assert output.exists()
    try:
        export_family_audit(database, state_db, output)
    except ValueError as exc:
        assert "already exists" in str(exc)
    else:
        raise AssertionError("audit export overwrote an existing report")
