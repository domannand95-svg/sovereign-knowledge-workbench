"""A re-run after an interrupted import never crashes and never pairs a source with another file's text.

Found by independent integration review (2026-11-20): `archive-batch-create` matched pending
sources to the current scan by relative path alone, across every registered root. After an
import was killed between admission and batch commit:
- a pending file that was then deleted made every re-run fail with a KeyError;
- a pending file that was then edited had the new text's summary stored against the old hash;
- a pending file in another root picked up a same-named file's text from this root.
"""
import json
import sqlite3

import pytest

from sovereign_workbench.cli import main
from sovereign_workbench.intake import scan_files
from sovereign_workbench.review_batches import admit, connect


def _register(capsys, root, db):
    assert main(["archive-intake-register", str(root), "--state-db", str(db), "--purpose", "t",
                 "--include", ".txt"]) == 0
    capsys.readouterr()


def _admit_only(root, db):
    """What a kill after admission and before the batch commit leaves behind."""
    with connect(db) as database:
        admit(database, root, scan_files(root, include_suffixes={".txt"}))


def _create(capsys, root, db, *extra):
    code = main(["archive-batch-create", str(root), "--state-db", str(db), "--include", ".txt", *extra])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    return json.loads(captured.out), captured.err


def _summaries(db):
    with sqlite3.connect(db) as database:
        return dict(database.execute(
            "SELECT s.relative_path || '@' || substr(s.source_sha256,1,8), c.model_summary "
            "FROM archive_review_candidates c JOIN review_sources s USING(source_id)").fetchall())


@pytest.fixture
def docs(tmp_path, capsys):
    root = tmp_path / "docs"
    root.mkdir()
    (root / "keep.txt").write_text("alpha keep text\n", encoding="utf-8")
    (root / "gone.txt").write_text("bravo gone text\n", encoding="utf-8")
    (root / "note.txt").write_text("charlie original text\n", encoding="utf-8")
    db = tmp_path / "a.db"
    _register(capsys, root, db)
    return root, db


def test_rerun_after_a_pending_file_was_deleted_does_not_crash(docs, capsys):
    root, db = docs
    _admit_only(root, db)
    (root / "gone.txt").unlink()
    outcome, err = _create(capsys, root, db)
    assert outcome["candidates"] == 2
    assert [item["relative_path"] for item in outcome["stale_pending"]] == ["gone.txt"]
    assert "gone.txt" in err and "stay pending" in err
    assert outcome["status"]["pending"] == 1 and outcome["status"]["batched"] == 2


def test_an_edited_pending_file_never_gets_the_new_text(docs, capsys):
    root, db = docs
    _admit_only(root, db)
    (root / "note.txt").write_text("delta edited text\n", encoding="utf-8")
    outcome, _ = _create(capsys, root, db)
    summaries = _summaries(db)
    edited = [text for key, text in summaries.items() if key.startswith("note.txt@")]
    assert edited == ["delta edited text"]  # only the new source, with its own text
    assert [item["relative_path"] for item in outcome["stale_pending"]] == ["note.txt"]
    assert outcome["status"]["pending"] == 1  # the old version waits; it is not given new text


def test_pending_sources_of_another_root_are_left_alone(tmp_path, capsys):
    one, two = tmp_path / "one", tmp_path / "two"
    for root, text in ((one, "root one text"), (two, "root two text")):
        root.mkdir()
        (root / "same.txt").write_text(text, encoding="utf-8")
    (one / "only-one.txt").write_text("only in root one", encoding="utf-8")
    db = tmp_path / "a.db"
    _register(capsys, one, db)
    _register(capsys, two, db)
    _admit_only(one, db)  # root one interrupted after admission
    outcome, _ = _create(capsys, two, db)
    assert outcome["candidates"] == 1
    assert outcome["stale_pending"] == []  # root one's files are not this import's concern
    assert list(_summaries(db).values()) == ["root two text"]
    outcome, _ = _create(capsys, one, db)  # root one, re-run on its own, completes correctly
    assert outcome["candidates"] == 2
    assert sorted(_summaries(db).values()) == ["only in root one", "root one text", "root two text"]


def test_rerun_with_deduplication_still_batches_already_admitted_files(docs, capsys):
    root, db = docs
    _admit_only(root, db)
    outcome, _ = _create(capsys, root, db, "--deduplicate-hash")
    assert outcome["candidates"] == 3 and outcome["stale_pending"] == []


def test_deduplication_does_not_hide_a_renamed_pending_file(docs, capsys):
    # Found in review of the fix: the stale pending source's hash hid the renamed file,
    # so `moved.txt` was never admitted and nothing named it.
    root, db = docs
    _admit_only(root, db)
    (root / "gone.txt").rename(root / "moved.txt")
    outcome, _ = _create(capsys, root, db, "--deduplicate-hash")
    assert [item["relative_path"] for item in outcome["stale_pending"]] == ["gone.txt"]
    assert any(key.startswith("moved.txt@") for key in _summaries(db))


def test_deduplication_ignores_another_roots_pending_sources(tmp_path, capsys):
    # Found in review: root one's interrupted, since-deleted source hid root two's copy.
    one, two = tmp_path / "one", tmp_path / "two"
    for root in (one, two):
        root.mkdir()
        (root / "x.txt").write_text("shared bytes", encoding="utf-8")
    db = tmp_path / "a.db"
    _register(capsys, one, db)
    _register(capsys, two, db)
    _admit_only(one, db)
    (one / "x.txt").unlink()
    outcome, _ = _create(capsys, two, db, "--deduplicate-hash")
    assert outcome["admitted"] == 1 and outcome["candidates"] == 1
