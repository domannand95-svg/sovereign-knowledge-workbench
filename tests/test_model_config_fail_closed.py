"""Local-model configuration: invalid settings stop the command; an outage falls back visibly.

Rule (agreed 2026-11-20):
- local model not requested            -> deterministic classifier, model settings ignored;
- requested but misconfigured          -> refuse before any document is processed
  (non-loopback endpoint, missing SKW_MODEL_NAME, content ceiling or timeout out of range);
- requested, valid, but unavailable    -> deterministic fallback, recorded on every affected
  candidate and reported on stderr.
"""
import json
import socket
import sqlite3

import pytest

import sovereign_workbench.cli as cli
import sovereign_workbench.pipeline as pipeline
from sovereign_workbench.cli import main
from sovereign_workbench.pipeline import analyze_workspace


def _closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def archive(tmp_path, capsys):
    docs = tmp_path / "docs"
    docs.mkdir()
    for index in range(3):
        (docs / f"{index}.txt").write_bytes(f"Research note {index} about seedlings.\n".encode())
    db = tmp_path / "a.db"
    assert main(["archive-intake-register", str(docs), "--state-db", str(db), "--purpose", "t",
                 "--include", ".txt"]) == 0
    capsys.readouterr()
    return docs, db


def _no_scan(*args, **kwargs):
    raise AssertionError("documents were processed despite an invalid model configuration")


INVALID = [
    ({"SKW_MODEL_NAME": "m", "SKW_MODEL_ENDPOINT": "http://example.com/v1"}, "loopback"),
    ({"SKW_MODEL_NAME": "m", "SKW_MODEL_ENDPOINT": "http://localhost:11434/v1"}, "loopback"),
    ({"SKW_MODEL_NAME": "m", "SKW_MODEL_ENDPOINT": "ftp://127.0.0.1/v1"}, "http"),
    ({"SKW_MODEL_NAME": ""}, "SKW_MODEL_NAME"),
    ({"SKW_MODEL_NAME": "   "}, "SKW_MODEL_NAME"),
    ({"SKW_MODEL_NAME": "m", "SKW_MODEL_MAX_CHARS": "999"}, "SKW_MODEL_MAX_CHARS"),
    ({"SKW_MODEL_NAME": "m", "SKW_MODEL_MAX_CHARS": "20001"}, "SKW_MODEL_MAX_CHARS"),
]
INVALID_IDS = ["remote-host", "hostname", "scheme", "no-name", "blank-name", "ceiling-low", "ceiling-high"]


@pytest.mark.parametrize("env,message", INVALID, ids=INVALID_IDS)
def test_batch_create_refuses_invalid_model_configuration_before_processing(
        archive, capsys, monkeypatch, env, message):
    docs, db = archive
    monkeypatch.delenv("SKW_MODEL_NAME", raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(cli, "scan_files", _no_scan)
    assert main(["archive-batch-create", str(docs), "--state-db", str(db), "--include", ".txt",
                 "--local-model"]) == 3
    error = capsys.readouterr().err
    assert error.startswith("workbench failed closed: Local model configuration is invalid")
    assert message in error


@pytest.mark.parametrize("env,message", INVALID, ids=INVALID_IDS)
def test_scan_refuses_invalid_model_configuration_before_processing(
        tmp_path, monkeypatch, env, message):
    monkeypatch.delenv("SKW_MODEL_NAME", raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(pipeline, "scan_files", _no_scan)
    with pytest.raises(ValueError, match=message):
        analyze_workspace(tmp_path, use_local_model=True)


def test_model_settings_are_ignored_when_the_model_is_not_requested(archive, capsys, monkeypatch):
    docs, db = archive
    monkeypatch.setenv("SKW_MODEL_ENDPOINT", "http://example.com/v1")
    monkeypatch.setenv("SKW_MODEL_TIMEOUT", "not-a-number")
    monkeypatch.delenv("SKW_MODEL_NAME", raising=False)
    assert main(["archive-batch-create", str(docs), "--state-db", str(db), "--include", ".txt"]) == 0
    assert json.loads(capsys.readouterr().out)["model_failures"] == 0
    report = analyze_workspace(docs)  # scan without --local-model
    assert not [f for f in report.findings if f.kind == "model_failure"]


def test_unavailable_model_falls_back_and_says_so(archive, capsys, monkeypatch):
    docs, db = archive
    monkeypatch.setenv("SKW_MODEL_NAME", "m")
    monkeypatch.setenv("SKW_MODEL_ENDPOINT", f"http://127.0.0.1:{_closed_port()}/v1")
    monkeypatch.setenv("SKW_MODEL_TIMEOUT", "5")
    assert main(["archive-batch-create", str(docs), "--state-db", str(db), "--include", ".txt",
                 "--local-model"]) == 0
    captured = capsys.readouterr()
    outcome = json.loads(captured.out)
    assert (outcome["candidates"], outcome["model_failures"]) == (3, 3)
    assert "local model was unavailable for 3 of 3" in captured.err
    with sqlite3.connect(db) as database:
        reasons = [row[0] for row in database.execute("SELECT proposal_reason FROM archive_review_candidates")]
    assert len(reasons) == 3
    assert all("local model failed closed, deterministic fallback used" in r for r in reasons)
