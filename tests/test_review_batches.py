from pathlib import Path
import sqlite3
import csv
import json
import zipfile

import pytest

from sovereign_workbench.intake import scan_files
from sovereign_workbench.review_batches import (admit, connect, counts, create_next, export_review_csv,
    import_review_csv, store_candidates, export_research_tickets, export_staging_manifest, export_review_xlsx,
    export_training_split, relationship_counts, routing_counts, store_relationships)


def make_files(root: Path, count: int) -> None:
    for index in range(count):
        (root / f"record-{index:03}.txt").write_text(f"record {index}", encoding="utf-8")


def test_admission_is_hash_bound_and_idempotent(tmp_path: Path):
    source = tmp_path / "source"; source.mkdir(); make_files(source, 2)
    records = scan_files(source)
    with connect(tmp_path / "state.db") as database:
        assert admit(database, source, records) == 2
        assert admit(database, source, records) == 0
        assert counts(database) == {"pending": 2, "batched": 0, "reviewed": 0, "quarantined": 0, "batches": 0}


def test_next_batches_are_stable_non_overlapping_and_resumable(tmp_path: Path):
    source = tmp_path / "source"; source.mkdir(); make_files(source, 53)
    state = tmp_path / "state.db"
    with connect(state) as database:
        assert admit(database, source, scan_files(source)) == 53
        first = create_next(database, limit=25)
    with connect(state) as database:
        second = create_next(database, limit=25)
        third = create_next(database, limit=25)
        assert create_next(database, limit=25) is None
        assert [len(first["items"]), len(second["items"]), len(third["items"])] == [25, 25, 3]
        ids = [item["source_id"] for batch in (first, second, third) for item in batch["items"]]
        assert len(ids) == len(set(ids)) == 53
        assert counts(database)["batches"] == 3


def test_changed_source_is_new_provenance_without_overwriting_old(tmp_path: Path):
    source = tmp_path / "source"; source.mkdir(); path = source / "record.txt"
    path.write_text("first", encoding="utf-8")
    with connect(tmp_path / "state.db") as database:
        admit(database, source, scan_files(source)); first = create_next(database)
        path.write_text("second", encoding="utf-8")
        assert admit(database, source, scan_files(source)) == 1
        second = create_next(database)
        assert first["items"][0]["source_sha256"] != second["items"][0]["source_sha256"]
        assert database.execute("SELECT COUNT(*) FROM review_sources").fetchone()[0] == 2


def test_batch_records_and_membership_are_immutable(tmp_path: Path):
    source = tmp_path / "source"; source.mkdir(); make_files(source, 1)
    with connect(tmp_path / "state.db") as database:
        admit(database, source, scan_files(source)); batch = create_next(database)
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            database.execute("UPDATE review_batches SET item_count=2 WHERE batch_id=?", (batch["batch_id"],))
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            database.execute("DELETE FROM review_batch_items WHERE batch_id=?", (batch["batch_id"],))


def test_review_export_never_overwrites_and_import_is_hash_bound(tmp_path: Path):
    source = tmp_path / "source"; source.mkdir(); make_files(source, 1)
    output = tmp_path / "review.csv"
    with connect(tmp_path / "state.db") as database:
        admit(database, source, scan_files(source)); batch = create_next(database)
        export_review_csv(database, batch["batch_id"], output)
        with pytest.raises(ValueError, match="never overwritten"):
            export_review_csv(database, batch["batch_id"], output)
        with output.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream); fields = reader.fieldnames; rows = list(reader)
        rows[0].update({"decision": "NEEDS_RESEARCH", "confidence": "LOW",
                        "review_note": "Context is unclear",
                        "research_question": "What was this intended to establish?", "reviewer": "Dominic"})
        with output.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields); writer.writeheader(); writer.writerows(rows)
        assert import_review_csv(database, output) == 1
        assert import_review_csv(database, output) == 0


def test_review_import_rejects_identity_tampering(tmp_path: Path):
    source = tmp_path / "source"; source.mkdir(); make_files(source, 1)
    output = tmp_path / "review.csv"
    with connect(tmp_path / "state.db") as database:
        admit(database, source, scan_files(source)); batch = create_next(database)
        export_review_csv(database, batch["batch_id"], output)
        output.write_text(output.read_text(encoding="utf-8-sig").replace(batch["items"][0]["source_sha256"], "0" * 64), encoding="utf-8-sig")
        with pytest.raises(ValueError):
            import_review_csv(database, output)


def test_candidate_population_is_immutable_and_exported(tmp_path: Path):
    source = tmp_path / "source"; source.mkdir(); make_files(source, 1)
    output = tmp_path / "review.csv"
    with connect(tmp_path / "state.db") as database:
        admit(database, source, scan_files(source)); batch = create_next(database)
        item = batch["items"][0]
        candidate = {"source_id": item["source_id"], "model_summary": "Bounded summary",
                     "proposed_topic": "research", "proposed_maturity": "research",
                     "proposed_authority": "non_authoritative_candidate", "proposal_reason": "deterministic"}
        assert store_candidates(database, batch["batch_id"], [candidate]) == 1
        assert store_candidates(database, batch["batch_id"], [candidate]) == 0
        export_review_csv(database, batch["batch_id"], output)
        assert "Bounded summary" in output.read_text(encoding="utf-8-sig")


def test_exception_routing_separates_spot_check_manual_and_recovery(tmp_path: Path):
    source = tmp_path / "source"; source.mkdir()
    (source / "eligible.txt").write_text("Research evidence with a clear scope", encoding="utf-8")
    (source / "uncertain.txt").write_text("Unclear material", encoding="utf-8")
    (source / "broken.docx").write_text("not a zip archive", encoding="utf-8")
    with connect(tmp_path / "state.db") as database:
        records = scan_files(source, include_suffixes={".txt", ".docx"})
        admit(database, source, records); batch = create_next(database)
        candidates = []
        for item in batch["items"]:
            name = item["relative_path"]
            candidates.append({
                "source_id": item["source_id"],
                "model_summary": "Bounded candidate summary",
                "proposed_topic": "research",
                "proposed_maturity": "research",
                "proposed_authority": "non_authoritative_candidate",
                "proposal_reason": "test candidate",
                "classification_confidence": 0.91 if name == "eligible.txt" else 0.4,
                "classification_abstained": name == "uncertain.txt",
                "risk_flags": ["phone_number"] if name == "eligible.txt" else [],
            })
        assert store_candidates(database, batch["batch_id"], candidates) == 3
        assert routing_counts(database) == {
            "SPOT_CHECK_ELIGIBLE": 1,
            "MANUAL_REVIEW_REQUIRED": 1,
            "RECOVERY_REQUIRED": 1,
        }
        routes = dict(database.execute(
            "SELECT s.relative_path,r.review_route FROM archive_review_routes r "
            "JOIN review_sources s ON s.source_id=r.source_id"
        ))
        assert routes["eligible.txt"] == "SPOT_CHECK_ELIGIBLE"
        assert routes["uncertain.txt"] == "MANUAL_REVIEW_REQUIRED"
        assert routes["broken.docx"] == "RECOVERY_REQUIRED"
        reason = database.execute(
            "SELECT route_reasons FROM archive_review_routes r JOIN review_sources s "
            "ON s.source_id=r.source_id WHERE s.relative_path='eligible.txt'"
        ).fetchone()[0]
        assert "external disclosure remains blocked: phone_number" in reason
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            database.execute("UPDATE archive_review_routes SET review_route='MANUAL_REVIEW_REQUIRED'")


def test_blank_review_rows_require_no_human_decision(tmp_path: Path):
    source = tmp_path / "source"; source.mkdir(); make_files(source, 2)
    output = tmp_path / "review.csv"
    with connect(tmp_path / "state.db") as database:
        admit(database, source, scan_files(source)); batch = create_next(database)
        export_review_csv(database, batch["batch_id"], output)
        with output.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        assert all(row["decision"] == "" for row in rows)
        assert import_review_csv(database, output) == 0


def test_relationship_evidence_is_immutable_inert_and_exported(tmp_path: Path):
    source = tmp_path / "source"; source.mkdir(); make_files(source, 1)
    output = tmp_path / "review.csv"
    with connect(tmp_path / "state.db") as database:
        admit(database, source, scan_files(source)); batch = create_next(database)
        item = batch["items"][0]
        relationship = {
            "source_id": item["source_id"], "relationship_type": "POSSIBLE_PARENT",
            "related_relative_path": "parent.txt", "related_sha256": "f" * 64,
            "confidence": 0.8, "evidence": "filename-token similarity",
        }
        assert store_relationships(database, batch["batch_id"], [relationship]) == 1
        assert store_relationships(database, batch["batch_id"], [relationship]) == 0
        assert relationship_counts(database)["POSSIBLE_PARENT"] == 1
        assert database.execute(
            "SELECT authority FROM archive_review_relationships"
        ).fetchone()[0] == "NONE"
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            database.execute("DELETE FROM archive_review_relationships")
        export_review_csv(database, batch["batch_id"], output)
        assert "parent.txt" in output.read_text(encoding="utf-8-sig")


def test_review_outputs_are_inert_and_training_split_is_disjoint(tmp_path: Path):
    source = tmp_path / "source"; source.mkdir(); make_files(source, 20)
    review = tmp_path / "review.csv"
    with connect(tmp_path / "state.db") as database:
        admit(database, source, scan_files(source)); batch = create_next(database, limit=20)
        export_review_csv(database, batch["batch_id"], review)
        with review.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream); fields = reader.fieldnames; rows = list(reader)
        for index, row in enumerate(rows):
            row.update({"decision": "NEEDS_RESEARCH" if index == 0 else "APPROVE",
                        "topic": "Other", "maturity": "Research",
                        "authority": "No authority", "confidence": "LOW",
                        "privacy_status": "Reviewed", "canonical_status": "UNRESOLVED",
                        "review_note": "Human reviewed", "reviewer": "Dominic",
                        "research_question": "What evidence supports this?" if index == 0 else ""})
        with review.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields); writer.writeheader(); writer.writerows(rows)
        assert import_review_csv(database, review) == 20
        tickets = tmp_path / "tickets.jsonl"
        assert export_research_tickets(database, tickets) == 1
        assert json.loads(tickets.read_text(encoding="utf-8").strip())["authority"] == "NONE"
        manifest = export_staging_manifest(database, tmp_path / "manifest.json")
        assert manifest["execution_authorized"] is False
        assert len(manifest["items"]) == 19
        assert all(item["status"] == "PROPOSED" for item in manifest["items"])
        result = export_training_split(database, tmp_path / "train.jsonl", tmp_path / "eval.jsonl")
        assert result["train_examples"] + result["evaluation_examples"] == 20
        train_ids = {json.loads(line)["source_id"] for line in (tmp_path / "train.jsonl").read_text(encoding="utf-8").splitlines()}
        eval_ids = {json.loads(line)["source_id"] for line in (tmp_path / "eval.jsonl").read_text(encoding="utf-8").splitlines()}
        assert train_ids.isdisjoint(eval_ids)


def test_training_split_uses_latest_decision_without_source_leakage(tmp_path: Path):
    source = tmp_path / "source"; source.mkdir(); make_files(source, 20)
    review = tmp_path / "review.csv"
    with connect(tmp_path / "state.db") as database:
        admit(database, source, scan_files(source)); batch = create_next(database, limit=20)
        export_review_csv(database, batch["batch_id"], review)
        with review.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream); fields = reader.fieldnames; rows = list(reader)
        for row in rows:
            row.update({"decision": "APPROVE", "topic": "Other", "maturity": "Research",
                        "authority": "No authority", "confidence": "LOW",
                        "privacy_status": "Reviewed", "canonical_status": "UNRESOLVED",
                        "review_note": "Initial review", "reviewer": "Dominic"})
        with review.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields); writer.writeheader(); writer.writerows(rows)
        assert import_review_csv(database, review) == 20
        rows[0]["review_note"] = "Corrected review"
        with review.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields); writer.writeheader(); writer.writerows(rows)
        assert import_review_csv(database, review) == 1

        result = export_training_split(database, tmp_path / "train.jsonl", tmp_path / "eval.jsonl")
        assert result["train_examples"] + result["evaluation_examples"] == 20
        examples = []
        for path in (tmp_path / "train.jsonl", tmp_path / "eval.jsonl"):
            examples.extend(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines())
        assert len({example["source_id"] for example in examples}) == 20
        corrected = [example for example in examples if example["source_id"] == rows[0]["source_id"]]
        assert len(corrected) == 1
        assert corrected[0]["expected"]["review_note"] == "Corrected review"


def test_formatted_excel_export_is_valid_and_non_overwriting(tmp_path: Path):
    source = tmp_path / "source"; source.mkdir(); make_files(source, 2)
    output = tmp_path / "review.xlsx"
    with connect(tmp_path / "state.db") as database:
        admit(database, source, scan_files(source)); batch = create_next(database)
        assert export_review_xlsx(database, batch["batch_id"], output) == output
        with zipfile.ZipFile(output) as archive:
            assert "xl/workbook.xml" in archive.namelist()
            assert b"Review Ledger" in archive.read("xl/workbook.xml")
            assert b"Review Queue" in archive.read("xl/workbook.xml")
            assert b"Instructions" in archive.read("xl/workbook.xml")
        with pytest.raises(ValueError, match="never overwritten"):
            export_review_xlsx(database, batch["batch_id"], output)
