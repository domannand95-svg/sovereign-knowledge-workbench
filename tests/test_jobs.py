import sqlite3
from pathlib import Path

from sovereign_plugins.contracts import hash_file
from sovereign_workbench.jobs import connect, enqueue, recover_interrupted, run_pending, status_counts


def test_jobs_are_idempotent_and_resumable(tmp_path: Path):
    source = tmp_path / "note.md"
    source.write_text("Contact person@example.org", encoding="utf-8")
    with connect(tmp_path / "state.db") as database:
        first = enqueue(database, "privacy.detect", source, hash_file(source))
        second = enqueue(database, "privacy.detect", source, hash_file(source))
        assert first == second
        assert status_counts(database)["pending"] == 1
        assert run_pending(database) == {"completed": 1, "failed": 0}
        assert status_counts(database)["completed"] == 1


def test_changed_source_fails_closed(tmp_path: Path):
    source = tmp_path / "note.md"
    source.write_text("first", encoding="utf-8")
    with connect(tmp_path / "state.db") as database:
        enqueue(database, "privacy.detect", source, hash_file(source))
        source.write_text("changed", encoding="utf-8")
        assert run_pending(database) == {"completed": 0, "failed": 1}


def test_running_job_is_recovered_after_interruption(tmp_path: Path):
    source = tmp_path / "note.md"
    source.write_text("text", encoding="utf-8")
    with connect(tmp_path / "state.db") as database:
        job_id = enqueue(database, "privacy.detect", source, hash_file(source))
        database.execute("UPDATE jobs SET status='running' WHERE job_id=?", (job_id,))
        database.commit()
        assert recover_interrupted(database) == 1
        assert status_counts(database)["pending"] == 1
