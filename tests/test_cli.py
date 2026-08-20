from pathlib import Path

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
