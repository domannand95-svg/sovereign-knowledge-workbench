"""Find and Review: intake -> review -> search, with hash-verified retrieval."""
import csv
import json
import threading
import urllib.error
import urllib.request

import pytest

from sovereign_workbench.cli import main
from sovereign_workbench.local_review_web import make_server
from sovereign_workbench.review_batches import connect, counts, import_review_csv
from sovereign_workbench.search import connect_readonly, search_archive


def _run(capsys, *argv):
    assert main(list(argv)) == 0, capsys.readouterr().err
    return json.loads(capsys.readouterr().out)


@pytest.fixture
def archive(tmp_path, capsys):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "soil.md").write_text(
        "# Soil inoculants\nMycorrhizal fungi improved seedling survival.\n", encoding="utf-8")
    (docs / "cuttings.txt").write_text("Grevillea cuttings rooted under mist.\n", encoding="utf-8")
    db = tmp_path / "state" / "archive.db"
    _run(capsys, "archive-intake-register", str(docs), "--state-db", str(db),
         "--purpose", "synthetic", "--include", ".md", ".txt")
    batch = _run(capsys, "archive-batch-create", str(docs), "--state-db", str(db),
                 "--include", ".md", ".txt")
    return docs, db, batch["batch"]["batch_id"]


def _decide_via_browser_screen(db, decision, note):
    server, token = make_server(db, "Synthetic Reviewer")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"

    def call(method, path, body=None):
        request = urllib.request.Request(
            base + path, method=method, data=json.dumps(body).encode() if body else None,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    try:
        _, pending = call("GET", "/api/next")
        item = pending["item"]
        status, reply = call("POST", "/api/decision", {
            "source_id": item["source_id"], "decision": decision, "note": note,
            "research_question": item["research_question"]})
        assert (status, reply) == (200, {"recorded": True})
        return item
    finally:
        server.shutdown()
        server.server_close()


def test_import_review_search_journey(archive, capsys):
    docs, db, _ = archive
    status = _run(capsys, "archive-batch-status", "--state-db", str(db))["status"]
    assert (status["batched"], status["reviewed"]) == (2, 0)

    reviewed = _decide_via_browser_screen(db, "APPROVE", "matches the nursery log")
    status = _run(capsys, "archive-batch-status", "--state-db", str(db))["status"]
    assert (status["batched"], status["reviewed"]) == (1, 1)

    found = _run(capsys, "archive-search", "seedling SURVIVAL", "--state-db", str(db))
    assert found["authority"] == "NONE" and found["unverified_sources"] == []
    [hit] = found["results"]
    assert hit["relative_path"] == "soil.md" and hit["verification"] == "VERIFIED"
    assert hit["content_matches"] == [
        {"line": 2, "text": "Mycorrhizal fungi improved seedling survival."}]
    assert hit["intake_root"] == str(docs.resolve()) and hit["intake_root_id"].startswith("IR-")
    if reviewed["source_path"] == "soil.md":
        assert hit["latest_decision"]["decision"] == "APPROVE"
        assert hit["latest_decision"]["reviewer"] == "Synthetic Reviewer"


def test_changed_source_is_flagged_and_its_text_is_not_returned(archive):
    docs, db, _ = archive
    (docs / "soil.md").write_text("Mycorrhizal fungi edited after intake.\n", encoding="utf-8")
    with connect(db) as database:
        result = search_archive(database, "edited after intake")
    assert result["results"] == []
    assert result["unverified_sources"] == [{
        "intake_root": str(docs.resolve()), "relative_path": "soil.md",
        "source_id": result["unverified_sources"][0]["source_id"],
        "status": "CHANGED_SINCE_INTAKE"}]


def test_changed_source_keeps_metadata_match_but_no_content(archive):
    docs, db, _ = archive
    (docs / "soil.md").write_text("replaced\n", encoding="utf-8")
    with connect(db) as database:
        [hit] = search_archive(database, "soil.md")["results"]
    assert hit["verification"] == "CHANGED_SINCE_INTAKE"
    assert hit["content_verified"] is False and hit["content_matches"] == []
    assert hit["metadata_matches"] == ["relative_path"]


def test_missing_source_is_reported_not_dropped(archive):
    docs, db, _ = archive
    (docs / "cuttings.txt").unlink()
    with connect(db) as database:
        result = search_archive(database, "Grevillea")
    [hit] = result["results"]  # still found through its recorded summary, labelled as such
    assert hit["verification"] == "MISSING" and hit["content_matches"] == []
    assert hit["metadata_matches"] == ["model_summary"]
    assert [(s["relative_path"], s["status"]) for s in result["unverified_sources"]] == [
        ("cuttings.txt", "MISSING")]


def test_search_rejects_empty_or_control_text_and_does_not_create_a_database(tmp_path, capsys):
    db = tmp_path / "absent.db"
    assert main(["archive-search", "x", "--state-db", str(db)]) == 3
    assert not db.exists()
    capsys.readouterr()
    with connect(tmp_path / "empty.db") as database:
        for query in ("", "   ", "a\x00b", "x" * 501):
            with pytest.raises(ValueError, match="printable"):
                search_archive(database, query)


def test_status_is_derived_from_latest_decision(archive):
    _, db, batch_id = archive
    with connect(db) as database:
        source_id, sha = database.execute(
            "SELECT source_id,source_sha256 FROM review_sources ORDER BY relative_path").fetchone()
        for decision, note, at in (("APPROVE", "first", "2026-11-19T10:00:00.500000Z"),
                                   ("QUARANTINE", "second", "2026-11-19T10:00:01.000000Z")):
            database.execute(
                "INSERT INTO archive_review_decisions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (note, source_id, batch_id, decision, "Other", "Research",
                 "Non-authoritative candidate", "LOW", "", "", note, "", "", "", "R", at))
        database.commit()
        assert counts(database) == {"pending": 0, "batched": 1, "reviewed": 0,
                                    "quarantined": 1, "batches": 1}
        stored = {row[0] for row in database.execute("SELECT state FROM review_sources")}
        assert stored == {"batched"}  # no redundant flag is written


def test_review_import_reports_every_problem_and_imports_nothing(archive, tmp_path, capsys):
    _, db, batch_id = archive
    exported = tmp_path / "review.csv"
    _run(capsys, "archive-batch-export", batch_id, "--state-db", str(db), "--output", str(exported))
    with exported.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        fields, rows = reader.fieldnames, list(reader)
    rows[0].update(decision="APPROVE", topic="horticulture", maturity="Research",
                   authority="Non-authoritative candidate", confidence="0.8",
                   reviewer="R", review_note="")
    rows[1].update(decision="NEEDS_RESEARCH", topic="Other", maturity="Research",
                   authority="Non-authoritative candidate", confidence="LOW",
                   reviewer="R", review_note="n", research_question="")
    edited = tmp_path / "edited.csv"
    with edited.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    with connect(db) as database:
        with pytest.raises(ValueError) as caught:
            import_review_csv(database, edited)
        message = str(caught.value)
        assert "4 problem(s); nothing was imported" in message
        assert f"row 2 ({rows[0]['source_path']}): topic 'horticulture' is not one of:" in message
        assert "Governance" in message and "confidence '0.8'" in message
        assert "review_note is required" in message
        assert f"row 3 ({rows[1]['source_path']}): NEEDS_RESEARCH requires a research question" in message
        assert database.execute("SELECT COUNT(*) FROM archive_review_decisions").fetchone()[0] == 0


def test_docx_is_searched_from_the_verified_bytes(tmp_path, capsys):
    import zipfile
    docs = tmp_path / "docs"
    docs.mkdir()
    body = ('<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body><w:p><w:r><w:t>Field trial of kangaroo grass</w:t></w:r></w:p></w:body></w:document>")
    with zipfile.ZipFile(docs / "trial.docx", "w") as archive:
        archive.writestr("word/document.xml", body)
    db = tmp_path / "a.db"
    _run(capsys, "archive-intake-register", str(docs), "--state-db", str(db),
         "--purpose", "synthetic", "--include", ".docx")
    _run(capsys, "archive-batch-create", str(docs), "--state-db", str(db), "--include", ".docx")
    [hit] = _run(capsys, "archive-search", "kangaroo", "--state-db", str(db))["results"]
    assert hit["content_matches"] == [{"line": 1, "text": "Field trial of kangaroo grass"}]


def test_match_beyond_200k_characters_is_found(tmp_path, capsys):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "long.txt").write_text(("x" * 99 + "\n") * 2100 + "needle deep\n", encoding="utf-8")
    db = tmp_path / "a.db"
    _run(capsys, "archive-intake-register", str(docs), "--state-db", str(db),
         "--purpose", "synthetic", "--include", ".txt")
    _run(capsys, "archive-batch-create", str(docs), "--state-db", str(db), "--include", ".txt")
    [hit] = _run(capsys, "archive-search", "needle", "--state-db", str(db))["results"]
    assert hit["content_matches"] == [{"line": 2101, "text": "needle deep"}]


def test_result_limit_never_hides_unverified_sources(tmp_path, capsys):
    docs = tmp_path / "docs"
    docs.mkdir()
    for name in ("a.txt", "b.txt", "c.txt", "z_changed.txt"):
        (docs / name).write_text("needle\n", encoding="utf-8")
    db = tmp_path / "a.db"
    _run(capsys, "archive-intake-register", str(docs), "--state-db", str(db),
         "--purpose", "synthetic", "--include", ".txt")
    _run(capsys, "archive-batch-create", str(docs), "--state-db", str(db), "--include", ".txt")
    (docs / "z_changed.txt").write_text("tampered needle\n", encoding="utf-8")
    found = _run(capsys, "archive-search", "needle", "--state-db", str(db), "--limit", "2")
    assert len(found["results"]) == 2 and found["truncated"] is True
    assert found["matching_sources"] == 4  # the changed file still matches on its summary
    assert [(s["relative_path"], s["status"]) for s in found["unverified_sources"]] == [
        ("z_changed.txt", "CHANGED_SINCE_INTAKE")]


def test_search_does_not_change_the_database(archive, capsys):
    import hashlib
    import sqlite3
    import gc
    _, db, _ = archive
    gc.collect()  # release connections left open by earlier CLI calls in this process
    with sqlite3.connect(db) as database:
        database.execute("PRAGMA journal_mode=DELETE")
    database.close()
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    _run(capsys, "archive-search", "seedling", "--state-db", str(db))
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
    with connect_readonly(db) as database:
        with pytest.raises(sqlite3.OperationalError):
            database.execute("CREATE TABLE should_fail(x)")


def test_snippet_line_numbers_and_bom(tmp_path, capsys):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.txt").write_bytes(
        b"\xef\xbb\xbfneedle bom\r\nsecond\x0cthird needle\r\n" + b"y" * 400 + b" needle end\n")
    (docs / "empty.txt").write_bytes(b"\xff\xfe not utf-8")
    db = tmp_path / "a.db"
    _run(capsys, "archive-intake-register", str(docs), "--state-db", str(db),
         "--purpose", "synthetic", "--include", ".txt")
    _run(capsys, "archive-batch-create", str(docs), "--state-db", str(db), "--include", ".txt")
    found = _run(capsys, "archive-search", "needle", "--state-db", str(db))
    [hit] = found["results"]
    lines = [(m["line"], m["text"]) for m in hit["content_matches"]]
    assert lines[0] == (1, "needle bom")
    assert lines[1] == (2, "second\x0cthird needle")
    assert lines[2][0] == 3 and "needle end" in lines[2][1] and lines[2][1].startswith("…")
    assert found["unverified_sources"] == []
    assert [(s["relative_path"], s["status"]) for s in found["not_searchable_sources"]] == [
        ("empty.txt", "NO_SEARCHABLE_TEXT")]


def test_duplicate_rows_for_one_source_are_rejected(archive, tmp_path, capsys):
    _, db, batch_id = archive
    exported = tmp_path / "review.csv"
    _run(capsys, "archive-batch-export", batch_id, "--state-db", str(db), "--output", str(exported))
    with exported.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        fields, rows = reader.fieldnames, list(reader)
    decided = []
    for decision in ("APPROVE", "QUARANTINE"):
        row = dict(rows[0])
        row.update(decision=decision, topic="Other", maturity="Research",
                   authority="Non-authoritative candidate", confidence="LOW",
                   reviewer="R", review_note=decision)
        decided.append(row)
    edited = tmp_path / "dup.csv"
    with edited.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(decided)
    with connect(db) as database:
        with pytest.raises(ValueError, match="already has a decision on row 2"):
            import_review_csv(database, edited)
