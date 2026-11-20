"""The review screen must never get stuck on one file.

Reproduces three blockers found in a synthetic walkthrough on 2026-11-20:
- a file that was moved, renamed or deleted after import stayed first in the queue forever;
- a file whose bytes still match the import but has no readable text (the "Text could not
  be read - check the file" route, served first) could never be decided in the page;
- the queue followed batch-id hash order instead of the order batches were created.
"""
import json
import sqlite3
import threading
import urllib.error
import urllib.request

import pytest

from sovereign_workbench.cli import main
from sovereign_workbench.local_review import ReviewController
from sovereign_workbench.local_review_web import HTML, make_server
from sovereign_workbench.review_batches import connect
from sovereign_workbench.search import read_verified_text


def _run(capsys, *argv):
    assert main(list(argv)) == 0, capsys.readouterr().err
    return json.loads(capsys.readouterr().out)


@pytest.fixture
def archive(tmp_path, capsys):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a-first.txt").write_bytes(b"Seedling survival notes for plot A.\n")
    (docs / "b-second.txt").write_bytes(b"Compost temperature log for week two.\n")
    (docs / "c-unreadable.txt").write_bytes(b"\xff\xfe\x00 not valid text\n")
    db = tmp_path / "archive.db"
    _run(capsys, "archive-intake-register", str(docs), "--state-db", str(db),
         "--purpose", "synthetic", "--include", ".txt")
    _run(capsys, "archive-batch-create", str(docs), "--state-db", str(db), "--include", ".txt")
    return docs, db


@pytest.fixture
def screen(archive):
    docs, db = archive
    server, token = make_server(db, "Synthetic Reviewer")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"

    def call(method, path, body=None):
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
        request = urllib.request.Request(base + path, method=method, headers=headers,
                                         data=None if body is None else json.dumps(body).encode())
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    yield docs, db, call
    server.shutdown()
    server.server_close()
    thread.join(timeout=3)


def _decide(call, item, decision="QUARANTINE"):
    return call("POST", "/api/decision", {"source_id": item["source_id"], "decision": decision,
                                          "note": "synthetic check", "research_question": ""})


def _decision_count(db):
    with connect(db) as database:
        return database.execute("SELECT COUNT(*) FROM archive_review_decisions").fetchone()[0]


def test_file_with_no_readable_text_is_hash_verified_and_decidable(screen):
    docs, db, call = screen
    item = call("GET", "/api/next")[1]["item"]
    assert item["source_path"] == "c-unreadable.txt"  # recovery items come first
    status, shown = call("GET", f"/api/source?source_id={item['source_id']}")
    assert status == 200
    assert shown["verified"] is False and shown["text"] is None
    assert shown["hash_verified"] is True and shown["status"] == "NO_SEARCHABLE_TEXT"
    assert _decide(call, item) == (200, {"recorded": True})


def test_page_enables_decisions_for_hash_verified_files_without_text():
    # The page decides from hash_verified, not from whether text could be shown.
    assert "data.hash_verified===true" in HTML
    assert "no readable text could be extracted" in HTML


def test_missing_file_can_be_skipped_and_the_queue_continues(screen):
    docs, db, call = screen
    first = call("GET", "/api/next")[1]["item"]
    assert _decide(call, first)[0] == 200
    current = call("GET", "/api/next")[1]
    assert current["item"]["source_path"] == "a-first.txt" and current["remaining"] == 2
    (docs / "a-first.txt").rename(docs / "a-renamed.txt")

    status, reply = _decide(call, current["item"])
    assert status == 400
    assert "no longer at its imported location" in reply["error"]
    assert "WinError" not in reply["error"] and "Errno" not in reply["error"]

    assert call("POST", "/api/skip", {"source_id": current["item"]["source_id"]}) == (200, {"skipped": True})
    after = call("GET", "/api/next")[1]
    assert after["item"]["source_path"] == "b-second.txt"
    assert (after["remaining"], after["skipped"]) == (1, 1)
    assert _decide(call, after["item"])[0] == 200

    done = call("GET", "/api/next")[1]
    assert (done["item"], done["remaining"], done["skipped"]) == (None, 0, 1)
    assert _decision_count(db) == 2  # skipping records nothing

    assert call("POST", "/api/skipped/reset", {}) == (200, {"skipped": 0})
    # Still missing: since the 2026-11-20 usability batch it is set aside and listed rather
    # than served first again; nothing is recorded for it.
    again = call("GET", "/api/next")[1]
    assert again["item"] is None and again["skipped"] == 0
    assert [(a["source_path"], a["status"]) for a in again["set_aside"]] == [("a-first.txt", "MISSING")]
    (docs / "a-renamed.txt").rename(docs / "a-first.txt")  # restored: it returns
    call("POST", "/api/skipped/reset", {})
    restored = call("GET", "/api/next")[1]
    assert restored["item"]["source_path"] == "a-first.txt" and restored["set_aside"] == []
    assert _decision_count(db) == 2


def test_only_the_current_file_can_be_skipped(screen):
    _, db, call = screen
    first = call("GET", "/api/next")[1]["item"]
    with connect(db) as database:
        other = database.execute("SELECT source_id FROM review_sources WHERE source_id<>?",
                                 (first["source_id"],)).fetchone()[0]
    status, reply = call("POST", "/api/skip", {"source_id": other})
    assert status == 400 and "reload" in reply["error"]
    assert call("POST", "/api/skip", {"source_id": first["source_id"], "x": 1})[0] == 400
    assert call("GET", "/api/next")[1]["item"]["source_id"] == first["source_id"]


def test_skipped_file_is_not_served_or_decidable_until_reset(screen):
    _, db, call = screen
    first = call("GET", "/api/next")[1]["item"]
    call("POST", "/api/skip", {"source_id": first["source_id"]})
    assert call("GET", f"/api/source?source_id={first['source_id']}")[0] == 409
    assert _decide(call, first)[0] == 400
    assert _decision_count(db) == 0


def test_busy_database_gives_a_clear_retryable_error(screen, monkeypatch):
    _, db, call = screen
    item = call("GET", "/api/next")[1]["item"]
    import sovereign_workbench.local_review_web as web

    def busy(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(web.ReviewController, "accept", busy)
    status, reply = _decide(call, item)
    assert status == 503 and "busy" in reply["error"]


def test_controller_reports_missing_file_in_plain_words(archive):
    docs, db = archive
    with connect(db) as database:
        controller = ReviewController(database, "Synthetic Reviewer")
        item = next(i for i in controller.pending() if i.source_path == "b-second.txt")
        (docs / "b-second.txt").unlink()
        with pytest.raises(ValueError, match="no longer at its imported location"):
            controller.accept(item, "REJECT", note="gone")


def test_queue_follows_batch_creation_order(tmp_path, capsys):
    docs = tmp_path / "docs"
    docs.mkdir()
    for index in range(8):
        (docs / f"n{index}.txt").write_bytes(f"Ordinary synthetic note number {index}.\n".encode())
    db = tmp_path / "archive.db"
    _run(capsys, "archive-intake-register", str(docs), "--state-db", str(db),
         "--purpose", "synthetic", "--include", ".txt")
    for _ in range(8):
        _run(capsys, "archive-batch-create", str(docs), "--state-db", str(db),
             "--include", ".txt", "--limit", "1")
    with connect(db) as database:
        by_sequence = [row[0] for row in database.execute(
            "SELECT batch_id FROM review_batches ORDER BY sequence")]
        assert len(by_sequence) == 8
        pending = [item.batch_id for item in ReviewController(database, "Synthetic Reviewer").pending()]
    assert pending == by_sequence


def test_hash_verified_flag_is_false_for_changed_and_missing(tmp_path):
    import hashlib
    (tmp_path / "a.txt").write_bytes(b"original")
    digest = hashlib.sha256(b"original").hexdigest()
    assert read_verified_text(str(tmp_path), "a.txt", digest)["hash_verified"] is True
    (tmp_path / "a.txt").write_bytes(b"changed")
    assert read_verified_text(str(tmp_path), "a.txt", digest)["hash_verified"] is False
    assert read_verified_text(str(tmp_path), "gone.txt", digest)["hash_verified"] is False
