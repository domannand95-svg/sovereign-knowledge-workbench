from pathlib import Path

import json

from sovereign_workbench.cli import main


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
