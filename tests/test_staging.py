import json
from pathlib import Path

import pytest

from sovereign_workbench.staging import StagingError, build_plan, connect, execute, recover, rollback, verify_plan


def _receipt(plan):
    return {"operation":"stage_copy", "target":plan["staged_path"], "proposal_sha256":"0" * 64}


def test_staging_copies_and_verifies_without_mutating_source(tmp_path: Path):
    source = tmp_path / "source.txt"; source.write_text("original", encoding="utf-8")
    root = tmp_path / "stage"; root.mkdir(); plan = build_plan(source, root)
    with connect(tmp_path / "journal.db") as database:
        rollback = execute(database, plan, _receipt(plan))
        assert source.read_text(encoding="utf-8") == "original"
        assert Path(plan["staged_path"]).read_text(encoding="utf-8") == "original"
        assert rollback["authority"] == "none"
        assert database.execute("SELECT status FROM staged_operations").fetchone()[0] == "staged"


def test_changed_source_fails_closed(tmp_path: Path):
    source = tmp_path / "source.txt"; source.write_text("first", encoding="utf-8")
    root = tmp_path / "stage"; root.mkdir(); plan = build_plan(source, root)
    source.write_text("changed", encoding="utf-8")
    with connect(tmp_path / "journal.db") as database:
        with pytest.raises(StagingError, match="changed"):
            execute(database, plan, _receipt(plan))
        assert not Path(plan["staged_path"]).exists()


def test_plan_tampering_and_interrupted_recovery_fail_closed(tmp_path: Path):
    source = tmp_path / "source.txt"; source.write_text("data", encoding="utf-8")
    root = tmp_path / "stage"; root.mkdir(); plan = build_plan(source, root)
    altered = dict(plan); altered["source_path"] = str(tmp_path / "other")
    with pytest.raises(StagingError): verify_plan(altered)
    with connect(tmp_path / "journal.db") as database:
        database.execute("INSERT INTO staged_operations(plan_id,plan_json,status) VALUES(?,?,?)",
                         (plan["plan_id"], json.dumps(plan, sort_keys=True, separators=(",", ":")), "staging"))
        database.commit(); assert recover(database) == 1
        assert database.execute("SELECT status FROM staged_operations").fetchone()[0] == "failed"


def test_rollback_requires_separate_binding_and_preserves_source(tmp_path: Path):
    source = tmp_path / "source.txt"; source.write_text("original", encoding="utf-8")
    root = tmp_path / "stage"; root.mkdir(); plan = build_plan(source, root)
    with connect(tmp_path / "journal.db") as database:
        execute(database, plan, _receipt(plan))
        with pytest.raises(StagingError, match="does not bind"):
            rollback(database, plan["plan_id"], {"operation":"rollback_stage_copy", "target":"wrong"})
        result = rollback(database, plan["plan_id"],
                          {"operation":"rollback_stage_copy", "target":plan["staged_path"]})
        assert result["status"] == "rolled_back"
        assert source.read_text(encoding="utf-8") == "original"
        assert not Path(plan["staged_path"]).exists()
