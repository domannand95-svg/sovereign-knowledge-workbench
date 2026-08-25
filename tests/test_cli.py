from pathlib import Path

import hashlib
import json
import sqlite3
from unittest.mock import patch

from sovereign_workbench.cli import main
from sovereign_workbench.model import Classification


def test_stdout_scan_is_read_only(tmp_path: Path, capsys):
    (tmp_path / "document.md").write_text("# Research\nStable evidence", encoding="utf-8")
    assert main(["scan", str(tmp_path)]) == 0
    assert '"authority":"review_only"' in capsys.readouterr().out


def test_output_fails_closed_without_authorizer(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.delenv("SKW_SOVEREIGN_AUTHORIZER", raising=False)
    (tmp_path / "document.md").write_text("# Research", encoding="utf-8")
    output = tmp_path.parent / "review.json"
    assert main(["scan", str(tmp_path), "--output", str(output), "--authorize-output"]) == 3
    assert not output.exists()
    assert "No Sovereign authorizer configured" in capsys.readouterr().err


def test_batch_counts_oversized_files_without_aborting(tmp_path: Path, capsys):
    (tmp_path / "large.md").write_bytes(b"x" * (1024 * 1024 + 1))
    (tmp_path / "small.md").write_text("research gap: verify source", encoding="utf-8")
    roles = tmp_path / "roles.json"
    roles.write_text(json.dumps({
        "contract_version": "sovereign.workbench.roles.v1",
        "roles": {"researcher": {"plugins": ["research.claims"], "human_approval": False}},
    }), encoding="utf-8")
    assert main([
        "plugin-batch", "research.claims", str(tmp_path),
        "--state-db", str(tmp_path / "jobs.db"),
        "--include", ".md", "--limit", "10", "--max-file-mb", "1",
        "--role", "researcher", "--roles", str(roles),
    ]) == 0
    outcome = json.loads(capsys.readouterr().out)
    assert outcome["completed"] == 1
    assert outcome["skipped_oversize"] == 1


def test_archive_batch_cli_resumes_and_exports_without_overwrite(tmp_path: Path, capsys):
    source = tmp_path / "source"; source.mkdir()
    for index in range(3):
        (source / f"{index}.txt").write_text(f"record {index}", encoding="utf-8")
    state = tmp_path / "review.db"; output = tmp_path / "batch.csv"
    args = ["archive-batch-create", str(source), "--state-db", str(state), "--include", ".txt", "--limit", "2"]
    assert main(args) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["batch"]["sequence"] == 1 and len(first["batch"]["items"]) == 2
    assert main(args) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["admitted"] == 0 and second["batch"]["sequence"] == 2
    assert main(["archive-batch-export", first["batch"]["batch_id"], "--state-db", str(state), "--output", str(output)]) == 0
    capsys.readouterr()
    assert output.exists()
    assert main(["archive-batch-export", first["batch"]["batch_id"], "--state-db", str(state), "--output", str(output)]) == 3
    assert "never overwritten" in capsys.readouterr().err


def test_archive_selection_manifest_admits_only_hash_bound_items(tmp_path: Path, capsys):
    source = tmp_path / "source"; source.mkdir()
    selected = source / "selected.txt"; selected.write_text("selected", encoding="utf-8")
    (source / "excluded.txt").write_text("excluded", encoding="utf-8")
    manifest = tmp_path / "selection.json"
    manifest.write_text(json.dumps({
        "contract_version": "sovereign.workbench.archive-selection.v1",
        "root": str(source.resolve()),
        "items": [{"relative_path": "selected.txt",
                   "sha256": hashlib.sha256(selected.read_bytes()).hexdigest()}],
    }), encoding="utf-8")
    assert main(["archive-batch-create", str(source), "--state-db", str(tmp_path / "review.db"),
                 "--include", ".txt", "--selection-manifest", str(manifest)]) == 0
    outcome = json.loads(capsys.readouterr().out)
    assert outcome["admitted"] == 1
    assert [item["relative_path"] for item in outcome["batch"]["items"]] == ["selected.txt"]


def test_archive_selection_manifest_rejects_hash_mismatch(tmp_path: Path, capsys):
    source = tmp_path / "source"; source.mkdir()
    (source / "selected.txt").write_text("selected", encoding="utf-8")
    manifest = tmp_path / "selection.json"
    manifest.write_text(json.dumps({
        "contract_version": "sovereign.workbench.archive-selection.v1",
        "root": str(source.resolve()),
        "items": [{"relative_path": "selected.txt", "sha256": "0" * 64}],
    }), encoding="utf-8")
    assert main(["archive-batch-create", str(source), "--state-db", str(tmp_path / "review.db"),
                 "--include", ".txt", "--selection-manifest", str(manifest)]) == 3
    assert "identity mismatch" in capsys.readouterr().err


def test_archive_local_model_candidate_binds_runtime_provenance(tmp_path: Path, monkeypatch, capsys):
    source = tmp_path / "source"; source.mkdir()
    (source / "selected.txt").write_text("research hypothesis", encoding="utf-8")
    state = tmp_path / "review.db"
    monkeypatch.setenv("SKW_MODEL_NAME", "test-model")
    monkeypatch.setenv("SKW_MODEL_MAX_CHARS", "4321")
    with patch("sovereign_workbench.cli.classify_with_local_model") as model:
        model.return_value = Classification("research", 0.9, (), "bounded", "local_model_candidate")
        assert main(["archive-batch-create", str(source), "--state-db", str(state),
                     "--include", ".txt", "--local-model"]) == 0
    capsys.readouterr()
    with sqlite3.connect(state) as database:
        reason = database.execute(
            "SELECT proposal_reason FROM archive_review_candidates"
        ).fetchone()[0]
    assert "model=test-model" in reason
    assert "max_content_chars=4321" in reason
