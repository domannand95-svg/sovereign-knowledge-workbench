import json
from pathlib import Path

import pytest
from sovereign_plugins.contracts import hash_file

from sovereign_workbench.jobs import connect, enqueue, run_pending
from sovereign_workbench.reviews import admit_completed, decide, list_candidates


def _candidate(tmp_path: Path):
    source = tmp_path / "note.md"
    source.write_text("research gap: verify source", encoding="utf-8")
    database = connect(tmp_path / "state.db")
    enqueue(database, "research.claims", source, hash_file(source))
    assert run_pending(database)["completed"] == 1
    assert admit_completed(database) == 1
    return database, list_candidates(database)[0]


def test_review_decision_is_append_only_and_non_authorizing(tmp_path: Path):
    database, candidate = _candidate(tmp_path)
    first = decide(database, candidate["candidate_id"], "needs_research", "operator", "citation missing")
    second = decide(database, candidate["candidate_id"], "approved", "operator", "citation verified")
    assert first != second
    assert database.execute("SELECT COUNT(*) FROM review_decisions").fetchone()[0] == 2
    assert list_candidates(database)[0]["decision"] == "approved"
    assert "authorized" not in list_candidates(database)[0]
    database.close()


def test_review_rejects_result_substitution(tmp_path: Path):
    database, candidate = _candidate(tmp_path)
    database.execute("UPDATE jobs SET result_json=? WHERE job_id=?", (json.dumps({"altered": True}), candidate["job_id"]))
    database.commit()
    with pytest.raises(ValueError, match="identity mismatch"):
        decide(database, candidate["candidate_id"], "approved", "operator", "looks valid")
    database.close()
