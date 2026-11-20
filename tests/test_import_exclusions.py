"""Files left out of an import are recorded durably, never silently dropped.

Found by the 4,100-file synthetic run (2026-11-20): a file over --max-file-mb was not
imported and nothing in the command output or archive-batch-status said so.
"""
import json
import sqlite3

import pytest

from sovereign_workbench.cli import main


@pytest.fixture
def archive(tmp_path, capsys):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "small.txt").write_bytes(b"ordinary note\n")
    (docs / "big.txt").write_bytes(b"x" * (2 * 1024 * 1024 + 10))
    db = tmp_path / "a.db"
    assert main(["archive-intake-register", str(docs), "--state-db", str(db), "--purpose", "t",
                 "--include", ".txt"]) == 0
    capsys.readouterr()
    return docs, db


def _create(capsys, docs, db, *extra):
    assert main(["archive-batch-create", str(docs), "--state-db", str(db), "--include", ".txt", *extra]) == 0
    captured = capsys.readouterr()
    return json.loads(captured.out), captured.err


def _status(capsys, db):
    assert main(["archive-batch-status", "--state-db", str(db)]) == 0
    return json.loads(capsys.readouterr().out)


def test_oversized_file_is_reported_and_recorded(archive, capsys):
    docs, db = archive
    outcome, err = _create(capsys, docs, db, "--max-file-mb", "1")
    assert outcome["candidates"] == 1
    [excluded] = outcome["excluded"]
    assert excluded == {"relative_path": "big.txt", "reason": "OVER_SIZE_LIMIT",
                        "size_bytes": 2 * 1024 * 1024 + 10, "limit_bytes": 1024 * 1024}
    assert "1 file was not imported" in err and "--max-file-mb" in err

    status = _status(capsys, db)  # visible after the command has finished
    assert status["excluded"]["count"] == 1
    assert status["excluded"]["files"][0]["relative_path"] == "big.txt"
    assert status["status"]["batched"] == 1  # an excluded file is never counted as batched or reviewed
    assert status["status"]["reviewed"] == 0


def test_exclusion_is_recorded_once_and_is_immutable(archive, capsys):
    docs, db = archive
    _create(capsys, docs, db, "--max-file-mb", "1")
    outcome, _ = _create(capsys, docs, db, "--max-file-mb", "1")  # re-run: still reported
    assert [item["relative_path"] for item in outcome["excluded"]] == ["big.txt"]
    with sqlite3.connect(db) as database:
        assert database.execute("SELECT COUNT(*) FROM archive_import_exclusions").fetchone()[0] == 1
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            database.execute("DELETE FROM archive_import_exclusions")
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            database.execute("UPDATE archive_import_exclusions SET reason='x'")


def test_later_import_with_a_larger_limit_clears_the_open_exclusion(archive, capsys):
    docs, db = archive
    _create(capsys, docs, db, "--max-file-mb", "1")
    outcome, err = _create(capsys, docs, db, "--max-file-mb", "5")
    assert outcome["excluded"] == [] and "not imported" not in err
    status = _status(capsys, db)
    assert status["excluded"]["count"] == 0  # history is kept; only open exclusions are listed
    assert status["status"]["batched"] == 2
    with sqlite3.connect(db) as database:
        assert database.execute("SELECT COUNT(*) FROM archive_import_exclusions").fetchone()[0] == 1


def test_file_that_grew_past_the_limit_after_import_stays_listed(tmp_path, capsys):
    # Independent review finding: an earlier admitted version must not hide a newer exclusion.
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "grow.txt").write_bytes(b"small at first\n")
    db = tmp_path / "a.db"
    assert main(["archive-intake-register", str(docs), "--state-db", str(db), "--purpose", "t",
                 "--include", ".txt"]) == 0
    capsys.readouterr()
    _create(capsys, docs, db, "--max-file-mb", "1")
    (docs / "grow.txt").write_bytes(b"y" * (2 * 1024 * 1024))
    outcome, _ = _create(capsys, docs, db, "--max-file-mb", "1")
    assert [item["relative_path"] for item in outcome["excluded"]] == ["grow.txt"]
    status = _status(capsys, db)
    assert status["excluded"]["count"] == 1
    assert status["excluded"]["files"][0]["size_bytes"] == 2 * 1024 * 1024


def test_unregistered_root_records_no_exclusion(tmp_path, capsys):
    # Independent review finding: nothing is written before the import's checks pass.
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "big.txt").write_bytes(b"x" * (2 * 1024 * 1024))
    db = tmp_path / "a.db"
    assert main(["archive-batch-create", str(docs), "--state-db", str(db), "--include", ".txt",
                 "--max-file-mb", "1"]) == 3
    assert "not registered" in capsys.readouterr().err
    with sqlite3.connect(db) as database:
        assert database.execute("SELECT COUNT(*) FROM archive_import_exclusions").fetchone()[0] == 0


def test_status_lists_one_row_per_excluded_path(archive, capsys, monkeypatch):
    import sovereign_workbench.review_batches as review_batches
    docs, db = archive
    monkeypatch.setattr(review_batches, "_now", lambda: "2026-11-20T00:00:00.000000Z")
    _create(capsys, docs, db, "--max-file-mb", "1")
    _create(capsys, docs, db, "--max-file-mb", "2")  # same path, same timestamp, different limit
    files = _status(capsys, db)["excluded"]["files"]
    assert [item["relative_path"] for item in files] == ["big.txt"]


def test_nothing_excluded_reports_an_empty_list(archive, capsys):
    docs, db = archive
    outcome, err = _create(capsys, docs, db)
    assert outcome["excluded"] == [] and err == ""
    assert _status(capsys, db)["excluded"] == {"count": 0, "files": []}
