"""CLI output encoding and up-front argument checks.

Reproduces two defects found in a synthetic walkthrough on 2026-11-20:
- with output redirected on Windows (cp1252), archive-search exited 3 ("failed closed")
  for any match in a document containing characters such as CJK or emoji, and accented
  text came out as cp1252 bytes rather than UTF-8;
- archive-batch-create rejected an out-of-range --limit only after scanning and
  extracting every file in the archive and admitting them all as pending.
"""
import json
import os
import sqlite3
import subprocess
import sys

import pytest

import sovereign_workbench.cli as cli
from sovereign_workbench.cli import main


def _register(docs, db, capsys):
    assert main(["archive-intake-register", str(docs), "--state-db", str(db), "--purpose", "t",
                 "--include", ".txt"]) == 0
    capsys.readouterr()


def test_search_output_is_utf8_even_when_the_console_encoding_is_not(tmp_path, capsys):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "notes.txt").write_text("日本語 seedling 🌱 café\n", encoding="utf-8")
    db = tmp_path / "a.db"
    _register(docs, db, capsys)
    assert main(["archive-batch-create", str(docs), "--state-db", str(db), "--include", ".txt"]) == 0
    capsys.readouterr()
    # Simulate redirected output on Windows, where Python would otherwise use cp1252.
    env = {**os.environ, "PYTHONIOENCODING": "cp1252"}
    env.pop("PYTHONUTF8", None)
    completed = subprocess.run(
        [sys.executable, "-m", "sovereign_workbench.cli", "archive-search", "seedling",
         "--state-db", str(db)],
        capture_output=True, env=env, timeout=120)
    assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")
    result = json.loads(completed.stdout.decode("utf-8"))
    [hit] = result["results"]
    assert hit["content_matches"][0]["text"] == "日本語 seedling 🌱 café"


def _no_scan(*args, **kwargs):
    raise AssertionError("the archive was scanned before the arguments were checked")


@pytest.mark.parametrize("limit", ["0", "1001"])
def test_batch_limit_is_checked_before_the_archive_is_scanned(tmp_path, capsys, monkeypatch, limit):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.txt").write_bytes(b"text\n")
    db = tmp_path / "a.db"
    _register(docs, db, capsys)
    monkeypatch.setattr(cli, "scan_files", _no_scan)
    assert main(["archive-batch-create", str(docs), "--state-db", str(db), "--include", ".txt",
                 "--limit", limit]) == 3
    assert "Batch limit must be between 1 and 1000" in capsys.readouterr().err
    with sqlite3.connect(db) as database:
        assert database.execute("SELECT COUNT(*) FROM review_sources").fetchone()[0] == 0


@pytest.mark.parametrize("name,value,message", [
    ("SKW_MODEL_TIMEOUT", "0", "SKW_MODEL_TIMEOUT is invalid"),
    ("SKW_MODEL_TIMEOUT", "soon", "SKW_MODEL_TIMEOUT is invalid"),
    ("SKW_MODEL_MAX_CHARS", "lots", "SKW_MODEL_MAX_CHARS is invalid"),
])
def test_model_settings_are_checked_before_the_archive_is_scanned(tmp_path, capsys, monkeypatch,
                                                                  name, value, message):
    monkeypatch.setenv(name, value)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.txt").write_bytes(b"text\n")
    db = tmp_path / "a.db"
    _register(docs, db, capsys)
    monkeypatch.setattr(cli, "scan_files", _no_scan)
    assert main(["archive-batch-create", str(docs), "--state-db", str(db), "--include", ".txt",
                 "--local-model"]) == 3
    assert message in capsys.readouterr().err


def test_bad_taxonomy_is_checked_before_the_archive_is_scanned(tmp_path, capsys, monkeypatch):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.txt").write_bytes(b"text\n")
    db = tmp_path / "a.db"
    _register(docs, db, capsys)
    monkeypatch.setattr(cli, "scan_files", _no_scan)
    assert main(["archive-batch-create", str(docs), "--state-db", str(db), "--include", ".txt",
                 "--taxonomy", str(tmp_path / "missing-taxonomy.json")]) == 3
    assert capsys.readouterr().err.startswith("workbench failed closed:")
