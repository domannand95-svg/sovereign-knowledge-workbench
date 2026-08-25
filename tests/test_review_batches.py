from pathlib import Path
import sqlite3
import csv

import pytest

from sovereign_workbench.intake import scan_files
from sovereign_workbench.review_batches import admit, connect, counts, create_next, export_review_csv, import_review_csv, store_candidates


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
