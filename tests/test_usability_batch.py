"""Usability issues found in the supervised synthetic trial (2026-11-20, main 5ef62c7).

1. archive-batch-status counted pending files without naming them or saying why.
2. Status and search printed only raw JSON.
3. A file changed or missing since import sat first in the review queue every session.
4. The screen said RESEARCH while records, CSV and docs say NEEDS_RESEARCH.
5. A renamed file showed only as missing; nothing pointed at its new path.
6. The oversized-file warning said "They are listed" for a single file.
"""
import csv
import json
import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest

from sovereign_workbench.cli import main
from sovereign_workbench.intake import scan_files
from sovereign_workbench.local_review_web import HTML, make_server
from sovereign_workbench.review_batches import admit, connect


def _run(capsys, *argv, code=0):
    result = main(list(argv))
    captured = capsys.readouterr()
    assert result == code, captured.err
    return captured


def _json(capsys, *argv):
    return json.loads(_run(capsys, *argv).out)


@pytest.fixture
def archive(tmp_path, capsys):
    docs = tmp_path / "docs"
    docs.mkdir()
    for name in ("alpha.txt", "bravo.txt", "charlie.txt", "delta.txt"):
        (docs / name).write_text(f"{name} seedling notes\n", encoding="utf-8")
    db = tmp_path / "a.db"
    _run(capsys, "archive-intake-register", str(docs), "--state-db", str(db), "--purpose", "t",
         "--include", ".txt")
    return docs, db


def _interrupted(docs, db):
    """What a killed import leaves: everything admitted, nothing batched."""
    with connect(db) as database:
        admit(database, docs, scan_files(docs, include_suffixes={".txt"}))


# 1. Pending files are named, with their state and a plain explanation ----------------------

def test_status_names_pending_files_and_why(archive, capsys):
    docs, db = archive
    _interrupted(docs, db)
    (docs / "bravo.txt").write_text("edited after admission\n", encoding="utf-8")
    (docs / "charlie.txt").unlink()
    pending = _json(capsys, "archive-batch-status", "--state-db", str(db))["pending"]
    states = {item["relative_path"]: item["state"] for item in pending["files"]}
    assert states == {"alpha.txt": "WAITING", "bravo.txt": "CHANGED_SINCE_ADMISSION",
                      "charlie.txt": "MISSING", "delta.txt": "WAITING"}
    assert pending["count"] == 4 and pending["by_state"] == {
        "WAITING": 2, "CHANGED_SINCE_ADMISSION": 1, "MISSING": 1}
    assert all(item["explanation"] for item in pending["files"])


# 2. Readable output, JSON kept for scripts --------------------------------------------------

def test_status_and_search_have_a_readable_form_and_keep_json(archive, capsys):
    docs, db = archive
    _run(capsys, "archive-batch-create", str(docs), "--state-db", str(db), "--include", ".txt",
         "--limit", "2")
    text = _run(capsys, "archive-batch-status", "--state-db", str(db), "--format", "text").out
    assert not text.lstrip().startswith("{")
    assert "Batched (waiting for a decision): 2" in text and "Waiting to be batched: 2" in text
    assert "charlie.txt" in text and "delta.txt" in text  # the waiting files are named
    found = _run(capsys, "archive-search", "seedling", "--state-db", str(db), "--format", "text").out
    assert not found.lstrip().startswith("{") and "alpha.txt" in found and "line 1" in found
    # Default output to a pipe or capture (not a terminal) stays JSON, unchanged for scripts.
    assert json.loads(_run(capsys, "archive-batch-status", "--state-db", str(db)).out)["status"]
    assert json.loads(_run(capsys, "archive-search", "seedling", "--state-db", str(db),
                           "--format", "json").out)["results"]


# 3. Changed or missing files are set aside visibly, never decided or dropped ----------------

@pytest.fixture
def screen(archive, capsys):
    docs, db = archive
    _run(capsys, "archive-batch-create", str(docs), "--state-db", str(db), "--include", ".txt")
    server, token = make_server(db, "Synthetic Reviewer")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"

    def call(method, path, body=None):
        request = urllib.request.Request(base + path, method=method,
                                         data=None if body is None else json.dumps(body).encode(),
                                         headers={"Authorization": f"Bearer {token}",
                                                  "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    yield docs, db, call
    server.shutdown()
    server.server_close()


def test_changed_files_are_set_aside_and_listed_not_first_every_time(screen):
    docs, db, call = screen
    _, first = call("GET", "/api/next")
    head = first["item"]["source_path"]
    (docs / head).write_text("changed during review\n", encoding="utf-8")
    status, data = call("GET", "/api/next")
    assert status == 200 and data["item"]["source_path"] != head
    assert [(item["source_path"], item["status"]) for item in data["set_aside"]] == [
        (head, "CHANGED_SINCE_INTAKE")]
    with connect(db) as database:  # nothing was recorded for the set-aside file
        assert database.execute("SELECT COUNT(*) FROM archive_review_decisions").fetchone()[0] == 0
    # A restored file comes back when the set-aside files are checked again.
    (docs / head).write_text(f"{head} seedling notes\n", encoding="utf-8")
    call("POST", "/api/skipped/reset", {})
    _, again = call("GET", "/api/next")
    assert again["item"]["source_path"] == head and again["set_aside"] == []


# 4. One name for the research decision ------------------------------------------------------

def test_research_decision_has_one_name_everywhere(screen, tmp_path):
    docs, db, call = screen
    assert 'data-choice="NEEDS_RESEARCH"' in HTML and ">NEEDS RESEARCH<" in HTML
    assert 'data-choice="RESEARCH"' not in HTML
    _, data = call("GET", "/api/next")
    status, result = call("POST", "/api/decision", {
        "source_id": data["item"]["source_id"], "decision": "NEEDS_RESEARCH",
        "note": "needs a source", "research_question": "Which source supports this?"})
    assert status == 200, result
    with connect(db) as database:
        assert database.execute("SELECT decision FROM archive_review_decisions").fetchone()[0] == "NEEDS_RESEARCH"


def test_csv_explains_the_research_decision_name(archive, capsys, tmp_path):
    docs, db = archive
    batch = _json(capsys, "archive-batch-create", str(docs), "--state-db", str(db), "--include", ".txt")["batch"]
    exported = tmp_path / "r.csv"
    _run(capsys, "archive-batch-export", batch["batch_id"], "--state-db", str(db), "--output", str(exported))
    with exported.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream); fields = reader.fieldnames; rows = list(reader)
    rows[0].update({"decision": "RESEARCH", "reviewer": "R", "review_note": "n"})
    with exported.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields); writer.writeheader(); writer.writerows(rows)
    err = _run(capsys, "archive-review-import", str(exported), "--state-db", str(db), code=3).err
    assert "use NEEDS_RESEARCH" in err


# 5. A renamed file points at its likely new path, without merging records -------------------

def test_missing_file_points_to_same_content_at_a_new_path(archive, capsys):
    docs, db = archive
    _run(capsys, "archive-batch-create", str(docs), "--state-db", str(db), "--include", ".txt")
    (docs / "alpha.txt").rename(docs / "alpha-renamed.txt")
    _run(capsys, "archive-batch-create", str(docs), "--state-db", str(db), "--include", ".txt")
    found = _json(capsys, "archive-search", "seedling", "--state-db", str(db))
    [missing] = [item for item in found["unverified_sources"] if item["relative_path"] == "alpha.txt"]
    assert missing["status"] == "MISSING"
    assert missing["same_content_at"] == ["alpha-renamed.txt"]
    with connect(db) as database:  # both records remain, separately
        assert database.execute("SELECT COUNT(*) FROM review_sources WHERE relative_path IN "
                                "('alpha.txt','alpha-renamed.txt')").fetchone()[0] == 2


def test_stale_pending_file_points_to_its_renamed_copy(archive, capsys):
    docs, db = archive
    _interrupted(docs, db)
    (docs / "alpha.txt").rename(docs / "alpha-renamed.txt")
    outcome = _json(capsys, "archive-batch-create", str(docs), "--state-db", str(db), "--include", ".txt")
    [stale] = outcome["stale_pending"]
    assert stale["relative_path"] == "alpha.txt" and stale["same_content_at"] == ["alpha-renamed.txt"]
    pending = _json(capsys, "archive-batch-status", "--state-db", str(db))["pending"]
    [item] = pending["files"]
    assert item["state"] == "MISSING" and item["same_content_at"] == ["alpha-renamed.txt"]


# 6. Singular warning ------------------------------------------------------------------------

def test_single_oversized_file_warning_is_singular(archive, capsys):
    docs, db = archive
    (docs / "big.txt").write_bytes(b"x" * (1024 * 1024 + 1))
    err = _run(capsys, "archive-batch-create", str(docs), "--state-db", str(db), "--include", ".txt",
               "--max-file-mb", "1").err
    assert "1 file was not imported because it is larger" in err
    assert "It is listed under 'excluded'" in err and "They are listed" not in err


def test_same_content_hint_never_points_at_a_copy_that_has_changed(archive, capsys):
    # Found in independent review: the hint checked only that the other path existed.
    docs, db = archive
    for name in ("a-one.txt", "b-two.txt", "c-three.txt"):
        (docs / name).write_text("identical body\n", encoding="utf-8")
    _run(capsys, "archive-batch-create", str(docs), "--state-db", str(db), "--include", ".txt")
    (docs / "a-one.txt").unlink()
    (docs / "b-two.txt").write_text("edited since import\n", encoding="utf-8")
    found = _json(capsys, "archive-search", "identical", "--state-db", str(db))
    [missing] = [item for item in found["unverified_sources"] if item["relative_path"] == "a-one.txt"]
    assert missing["same_content_at"] == ["c-three.txt"]
    text = _run(capsys, "archive-search", "identical", "--state-db", str(db), "--format", "text").out
    assert "or an identical copy" in text


def test_stale_hint_never_points_at_a_changed_copy(archive, capsys):
    docs, db = archive
    (docs / "copy.txt").write_text("alpha.txt seedling notes\n", encoding="utf-8")  # same as alpha
    _interrupted(docs, db)
    (docs / "alpha.txt").unlink()
    (docs / "copy.txt").write_text("changed copy\n", encoding="utf-8")
    pending = _json(capsys, "archive-batch-status", "--state-db", str(db))["pending"]
    [item] = [entry for entry in pending["files"] if entry["relative_path"] == "alpha.txt"]
    assert item["state"] == "MISSING" and item["same_content_at"] == []
