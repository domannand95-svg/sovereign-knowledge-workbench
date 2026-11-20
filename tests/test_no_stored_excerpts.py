"""No document text is stored by the review workflow, and stored excerpts in older
databases are never shown, exported or searched.

Found in the combined review (2026-11-20): every review candidate stored the first 280
characters of the document as `model_summary`. That text was written to the review
database, review CSV/XLSX exports, research tickets and the training split, shown on the
review screen and matched by search, even after the file had changed. Owner decision
(2026-11-20): keep the no-persistent-document-text contract and fix the implementation.

Each test plants a unique marker in synthetic documents and checks that it never appears
outside the original file.
"""
import json
import sqlite3
import threading
import urllib.request
from pathlib import Path

import pytest

from sovereign_workbench.analysis import NOT_STORED_SUMMARY, classify
from sovereign_workbench.cli import main
from sovereign_workbench import local_model
from sovereign_workbench.local_model import LocalModelConfig, classify_with_local_model
from sovereign_workbench.local_review import ReviewController
from sovereign_workbench.local_review_web import make_server
from sovereign_workbench.model import FileRecord
from sovereign_workbench.review_batches import (connect, export_review_csv, export_review_xlsx,
                                                store_candidates)

MARKER = "QUOKKA-LANTERN-7731"


def _run(capsys, *argv):
    code = main(list(argv))
    captured = capsys.readouterr()
    assert code == 0, captured.err
    return json.loads(captured.out) if captured.out.strip().startswith("{") else captured.out


def _database_bytes(db: Path) -> bytes:
    data = b""
    for suffix in ("", "-wal", "-journal"):
        path = Path(str(db) + suffix)
        if path.exists():
            data += path.read_bytes()
    return data


@pytest.fixture
def imported(tmp_path, capsys):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "field-notes.txt").write_text(f"Field notes {MARKER} about seedling survival.\n" * 3,
                                         encoding="utf-8")
    (docs / "empty.txt").write_bytes(b"")
    db = tmp_path / "archive.db"
    _run(capsys, "archive-intake-register", str(docs), "--state-db", str(db), "--purpose", "t",
         "--include", ".txt")
    outcome = _run(capsys, "archive-batch-create", str(docs), "--state-db", str(db), "--include", ".txt")
    return docs, db, outcome["batch"]["batch_id"]


def test_import_stores_no_document_text(imported):
    _docs, db, _batch = imported
    assert MARKER.encode() not in _database_bytes(db)
    with sqlite3.connect(db) as database:
        summaries = dict(database.execute(
            "SELECT s.relative_path,c.model_summary FROM archive_review_candidates c "
            "JOIN review_sources s USING(source_id)").fetchall())
    assert summaries == {"field-notes.txt": NOT_STORED_SUMMARY, "empty.txt": ""}


def test_routing_still_flags_files_without_text(imported):
    _docs, db, _batch = imported
    with sqlite3.connect(db) as database:
        reasons = dict(database.execute(
            "SELECT s.relative_path,r.route_reasons FROM archive_review_routes r "
            "JOIN review_sources s USING(source_id)").fetchall())
    assert "summary is empty" in reasons["empty.txt"]
    assert "summary is empty" not in reasons["field-notes.txt"]


def test_review_exports_contain_no_document_text(imported, tmp_path):
    _docs, db, batch_id = imported
    with connect(db) as database:
        export_review_csv(database, batch_id, tmp_path / "review.csv")
        export_review_xlsx(database, batch_id, tmp_path / "review.xlsx")
    assert MARKER not in (tmp_path / "review.csv").read_text(encoding="utf-8-sig")
    import zipfile
    with zipfile.ZipFile(tmp_path / "review.xlsx") as workbook:
        assert not any(MARKER.encode() in workbook.read(name) for name in workbook.namelist())


def test_scan_report_contains_no_document_text(imported, capsys, tmp_path):
    docs, _db, _batch = imported
    output = tmp_path / "scan-out"
    main(["scan", str(docs), "--include", ".txt", "--output", str(output)])
    captured = capsys.readouterr()
    assert MARKER not in captured.out
    written = [path for path in output.rglob("*") if path.is_file()] if output.exists() else []
    assert not any(MARKER.encode() in path.read_bytes() for path in written)
    assert classify(FileRecord("a.txt", "0" * 64, 1, "text/plain", 0, extracted_text=MARKER,
                               extraction_status="extracted")).summary == NOT_STORED_SUMMARY


def test_local_model_summary_and_free_text_labels_are_not_kept(monkeypatch):
    reply = {"choices": [{"message": {"content": json.dumps({
        "module": "research", "confidence": 0.9, "labels": ["research", f"quote {MARKER}"],
        "summary": f"The document says {MARKER}"})}}]}
    monkeypatch.setattr(local_model, "_post_json", lambda *a, **k: json.dumps(reply).encode())
    record = FileRecord("a.txt", "0" * 64, 1, "text/plain", 0, extracted_text=f"text {MARKER}",
                        extraction_status="extracted")
    config = LocalModelConfig(endpoint="http://127.0.0.1:9/v1/chat/completions", model="m")
    result = classify_with_local_model(record, config, allowed_modules=["research", "other"])
    assert result.module == "research"
    assert MARKER not in result.summary and MARKER not in " ".join(result.labels)
    assert result.labels == ("research",)


def test_store_candidates_never_persists_text_from_any_caller(tmp_path):
    from sovereign_workbench.intake import scan_files
    from sovereign_workbench.review_batches import admit, create_next, register_intake_root
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.txt").write_text("alpha", encoding="utf-8")
    db = tmp_path / "direct.db"
    with connect(db) as database:
        register_intake_root(database, docs, purpose="t", allowed_suffixes={".txt"})
        admit(database, docs, scan_files(docs))
        batch = create_next(database, limit=5)
        store_candidates(database, batch["batch_id"], [{
            "source_id": batch["items"][0]["source_id"], "model_summary": f"Text {MARKER}",
            "proposed_topic": "Other", "proposed_maturity": "Research",
            "proposed_authority": "None", "proposal_reason": "test"}])
        stored = database.execute("SELECT model_summary FROM archive_review_candidates").fetchone()[0]
    assert stored == NOT_STORED_SUMMARY
    assert MARKER.encode() not in _database_bytes(db)


# --- Older databases that already contain excerpts -------------------------------------

LEGACY = f"Legacy excerpt {MARKER} from before the fix"


@pytest.fixture
def legacy(tmp_path, capsys):
    """A database where a candidate row holds document text, as older versions wrote it."""
    docs = tmp_path / "legacy-docs"
    docs.mkdir()
    (docs / "note.txt").write_text("seedling survival notes\n", encoding="utf-8")
    db = tmp_path / "legacy.db"
    _run(capsys, "archive-intake-register", str(docs), "--state-db", str(db), "--purpose", "t",
         "--include", ".txt")
    with connect(db) as database:
        from sovereign_workbench.intake import scan_files
        from sovereign_workbench.review_batches import admit, create_next
        admit(database, docs, scan_files(docs, include_suffixes={".txt"}))
        batch = create_next(database, limit=5)
        item = batch["items"][0]
        database.execute(
            "INSERT INTO archive_review_candidates VALUES(?,?,?,?,?,?,?,?)",
            (item["source_id"], batch["batch_id"], LEGACY, "Other", "Research", "None", "legacy", "0" * 64))
        database.commit()
    return docs, db, batch["batch_id"]


def test_legacy_excerpt_is_not_exported(legacy, tmp_path):
    _docs, db, batch_id = legacy
    with connect(db) as database:
        export_review_csv(database, batch_id, tmp_path / "legacy.csv")
    assert MARKER not in (tmp_path / "legacy.csv").read_text(encoding="utf-8-sig")


def test_legacy_excerpt_is_not_searchable(legacy, capsys):
    _docs, db, _batch = legacy
    result = _run(capsys, "archive-search", MARKER, "--state-db", str(db))
    assert result["matching_sources"] == 0
    assert MARKER not in json.dumps(result, ensure_ascii=False).replace(json.dumps(MARKER), "")


def test_legacy_excerpt_is_not_shown_on_either_review_screen(legacy):
    _docs, db, _batch = legacy
    with connect(db) as database:
        items = ReviewController(database, "Synthetic Reviewer").pending()
        assert items and all(MARKER not in item.model_summary for item in items)
    server, token = make_server(db, "Synthetic Reviewer")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/api/next",
                                         headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
        assert MARKER not in body
    finally:
        server.shutdown()
        server.server_close()


def test_status_reports_whether_a_database_holds_document_text(imported, legacy, capsys):
    _docs, new_db, _batch = imported
    status = _run(capsys, "archive-batch-status", "--state-db", str(new_db))
    assert status["stored_document_text"]["contains_document_text"] is False
    _docs, old_db, _batch = legacy
    status = _run(capsys, "archive-batch-status", "--state-db", str(old_db))
    assert status["stored_document_text"] == {
        "contains_document_text": True,
        "rows": {"candidate_summaries": 1, "family_claims": 0, "family_claim_excerpts": 0,
                 "section_excerpts": 0}}
    assert MARKER not in json.dumps(status, ensure_ascii=False)  # counted, never shown
