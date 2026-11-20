"""archive-batch-create: one transaction, deterministic model fallback, exit 3 on lock timeout."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from sovereign_workbench import review_batches
from sovereign_workbench.cli import main
from sovereign_workbench.review_batches import BatchLockTimeoutError, _begin_batch_transaction, create_next

BATCH_TABLES = ("review_batches", "review_batch_items", "archive_review_candidates",
                "archive_review_routes", "archive_review_relationships")


def _setup(tmp_path: Path, capsys, files: int = 3) -> tuple[list[str], Path]:
    source = tmp_path / "source"; source.mkdir()
    for index in range(files):
        (source / f"{index}.txt").write_text(f"research note {index}", encoding="utf-8")
    (source / "copy.txt").write_text("research note 0", encoding="utf-8")  # gives a relationship
    state = tmp_path / "review.db"
    assert main(["archive-intake-register", str(source), "--state-db", str(state),
                 "--purpose", "test", "--include", ".txt"]) == 0
    capsys.readouterr()
    return ["archive-batch-create", str(source), "--state-db", str(state), "--include", ".txt"], state


def _state(state: Path) -> dict[str, int]:
    with sqlite3.connect(state) as database:
        result = {table: database.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in BATCH_TABLES}
        result["pending"] = database.execute("SELECT COUNT(*) FROM review_sources WHERE state='pending'").fetchone()[0]
    return result


def _nothing_written(state: Path, pending: int) -> None:
    observed = _state(state)
    assert observed["pending"] == pending
    assert all(observed[table] == 0 for table in BATCH_TABLES), observed


def test_model_failure_falls_back_and_completes_the_batch(tmp_path, capsys, monkeypatch):
    args, state = _setup(tmp_path, capsys)
    monkeypatch.setenv("SKW_MODEL_NAME", "m")
    # A valid loopback endpoint with nothing listening: the model is unavailable, not misconfigured.
    import socket
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]
    monkeypatch.setenv("SKW_MODEL_ENDPOINT", f"http://127.0.0.1:{closed_port}/v1")
    monkeypatch.setenv("SKW_MODEL_TIMEOUT", "5")
    assert main([*args, "--local-model"]) == 0
    outcome = json.loads(capsys.readouterr().out)
    assert outcome["model_failures"] == 4 and outcome["candidates"] == 4 and outcome["batch"]
    with sqlite3.connect(state) as database:
        reasons = [row[0] for row in database.execute("SELECT proposal_reason FROM archive_review_candidates")]
    assert len(reasons) == 4 and all("local model failed closed, deterministic fallback used" in r for r in reasons)
    assert main([*args, "--local-model"]) == 0  # rerun: nothing pending, no duplicate batch
    rerun = json.loads(capsys.readouterr().out)
    assert rerun["admitted"] == 0 and rerun["batch"] is None and _state(state)["review_batches"] == 1


@pytest.mark.parametrize("table", ["archive_review_candidates", "archive_review_routes",
                                   "archive_review_relationships"])
def test_storage_failure_part_way_leaves_nothing_and_rerun_completes(tmp_path, capsys, table):
    args, state = _setup(tmp_path, capsys)
    with sqlite3.connect(state) as database:
        database.execute(f"CREATE TRIGGER fail_{table} BEFORE INSERT ON {table} "
                         "BEGIN SELECT RAISE(ABORT, 'injected storage failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="injected storage failure"):
        main(args)
    capsys.readouterr()
    _nothing_written(state, pending=4)
    with sqlite3.connect(state) as database:
        database.execute(f"DROP TRIGGER fail_{table}")
    assert main(args) == 0
    outcome = json.loads(capsys.readouterr().out)
    assert outcome["candidates"] == 4 and outcome["relationships"] >= 1
    assert _state(state)["pending"] == 0


def test_lock_held_by_another_process_exits_3_and_writes_nothing(tmp_path, capsys):
    args, state = _setup(tmp_path, capsys)
    holder = sqlite3.connect(state, isolation_level=None)

    def classify_and_take_lock(record, taxonomy):
        if not holder.in_transaction:
            holder.execute("BEGIN IMMEDIATE")
        from sovereign_workbench.analysis import classify
        return classify(record, taxonomy)

    try:
        with patch("sovereign_workbench.cli.classify", side_effect=classify_and_take_lock):
            assert main(args) == 3
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err.startswith("workbench failed closed: ")
        assert "no batch was written" in captured.err
    finally:
        holder.rollback(); holder.close()
    _nothing_written(state, pending=4)
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["candidates"] == 4


def test_pending_set_changed_by_another_writer_exits_3_and_writes_nothing(tmp_path, capsys):
    args, state = _setup(tmp_path, capsys)

    def classify_while_another_writer_batches(record, taxonomy):
        other = review_batches.connect(state)
        try:
            if _state(state)["review_batches"] == 0:
                create_next(other, limit=1)
        finally:
            other.close()
        from sovereign_workbench.analysis import classify
        return classify(record, taxonomy)

    with patch("sovereign_workbench.cli.classify", side_effect=classify_while_another_writer_batches):
        assert main(args) == 3
    captured = capsys.readouterr()
    assert captured.out == "" and "pending sources changed" in captured.err
    observed = _state(state)
    assert observed["review_batches"] == 1 and observed["archive_review_candidates"] == 0
    assert observed["pending"] == 3  # only the competing writer's one-item batch exists


class _Connection:
    def __init__(self, code: int | None):
        self.code = code
        self.in_transaction = False

    def execute(self, sql):
        error = sqlite3.OperationalError("simulated")
        if self.code is not None:
            error.sqlite_errorcode = self.code
        raise error


def test_only_exact_busy_at_begin_becomes_a_lock_timeout():
    with pytest.raises(BatchLockTimeoutError):
        _begin_batch_transaction(_Connection(sqlite3.SQLITE_BUSY))
    for code in (sqlite3.SQLITE_LOCKED, 517, sqlite3.SQLITE_READONLY, sqlite3.SQLITE_IOERR, None):
        with pytest.raises(sqlite3.OperationalError) as caught:
            _begin_batch_transaction(_Connection(code))
        assert not isinstance(caught.value, BatchLockTimeoutError)
    assert not issubclass(sqlite3.OperationalError, ValueError)  # so these never map to exit 3
