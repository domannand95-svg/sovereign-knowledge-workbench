from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .plugins import PluginRuntimeError, run_plugin
from .runner_ownership import exclusive_runner, OwnershipLostError


SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  job_id TEXT PRIMARY KEY,
  plugin_id TEXT NOT NULL,
  source_path TEXT NOT NULL,
  source_sha256 TEXT NOT NULL,
  max_bytes INTEGER NOT NULL DEFAULT 104857600,
  status TEXT NOT NULL CHECK(status IN ('pending','running','completed','failed')),
  attempts INTEGER NOT NULL DEFAULT 0,
  runner_token TEXT,
  result_json TEXT,
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_status_idx ON jobs(status, created_at);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    database = sqlite3.connect(path)
    database.executescript(SCHEMA)
    columns = {row[1] for row in database.execute("PRAGMA table_info(jobs)")}
    if "max_bytes" not in columns:
        database.execute("ALTER TABLE jobs ADD COLUMN max_bytes INTEGER NOT NULL DEFAULT 104857600")
    if "runner_token" not in columns:
        database.execute("ALTER TABLE jobs ADD COLUMN runner_token TEXT")
    database.execute("PRAGMA journal_mode=WAL")
    from .reviews import install
    install(database)
    return database


def enqueue(
    database: sqlite3.Connection,
    plugin_id: str,
    source: Path,
    source_sha256: str,
    *,
    max_bytes: int = 100 * 1024 * 1024,
) -> str:
    if max_bytes < 1:
        raise ValueError("Job size limit must be positive")
    identity = hashlib.sha256(f"{plugin_id}\0{source.resolve()}\0{source_sha256}".encode()).hexdigest()
    now = _now()
    database.execute(
        "INSERT OR IGNORE INTO jobs(job_id,plugin_id,source_path,source_sha256,max_bytes,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (identity, plugin_id, str(source.resolve()), source_sha256, max_bytes, "pending", now, now),
    )
    database.commit()
    return identity


def recover_interrupted(database: sqlite3.Connection) -> int:
    with exclusive_runner(database):
        return _recover_interrupted_owned(database)


def _recover_interrupted_owned(database: sqlite3.Connection) -> int:
    cursor = database.execute(
        "UPDATE jobs SET status='pending', runner_token=NULL, error='interrupted_before_completion', updated_at=? WHERE status='running'",
        (_now(),),
    )
    database.commit()
    return cursor.rowcount


def run_pending(database: sqlite3.Connection, *, limit: int = 25) -> dict[str, int]:
    with exclusive_runner(database) as token:
        return _run_pending_owned(database, limit=limit, token=token)


def _run_pending_owned(database, *, limit, token):
    _recover_interrupted_owned(database)
    rows = database.execute(
        "SELECT job_id,plugin_id,source_path,source_sha256,max_bytes FROM jobs WHERE status='pending' ORDER BY created_at,job_id LIMIT ?",
        (limit,),
    ).fetchall()
    counts = {"completed": 0, "failed": 0}
    for job_id, plugin_id, source_path, expected_hash, max_bytes in rows:
        claim = database.execute(
            "UPDATE jobs SET status='running', attempts=attempts+1, runner_token=?, updated_at=? WHERE job_id=? AND status='pending'",
            (token, _now(), job_id),
        )
        database.commit()
        if claim.rowcount != 1:
            continue
        try:
            result = run_plugin(plugin_id, Path(source_path), max_bytes=max_bytes)
            if result["input_sha256"] != expected_hash:
                raise PluginRuntimeError("Source changed after job admission")
            status, encoded, error = "completed", json.dumps(result, sort_keys=True), None
        except (OSError, PluginRuntimeError, ValueError) as exc:
            status, encoded, error = "failed", None, str(exc)[:1000]
        completion = database.execute(
            "UPDATE jobs SET status=?,result_json=?,error=?,runner_token=NULL,updated_at=? "
            "WHERE job_id=? AND status='running' AND runner_token=?",
            (status, encoded, error, _now(), job_id, token),
        )
        if completion.rowcount != 1:
            database.rollback()
            raise OwnershipLostError("Job ownership changed before completion")
        database.commit()
        counts[status] += 1
    return counts


def status_counts(database: sqlite3.Connection) -> dict[str, int]:
    counts = {key: 0 for key in ("pending", "running", "completed", "failed")}
    for status, count in database.execute("SELECT status,COUNT(*) FROM jobs GROUP BY status"):
        counts[status] = count
    return counts


def failed_jobs(database: sqlite3.Connection, *, limit: int = 25) -> list[dict[str, object]]:
    if limit < 1 or limit > 100:
        raise ValueError("Failure query limit must be between 1 and 100")
    rows = database.execute(
        "SELECT job_id,plugin_id,source_path,attempts,error,updated_at "
        "FROM jobs WHERE status='failed' ORDER BY updated_at DESC,job_id LIMIT ?",
        (limit,),
    ).fetchall()
    return [
        {
            "job_id": job_id,
            "plugin_id": plugin_id,
            "source_path": source_path,
            "attempts": attempts,
            "error": error,
            "updated_at": updated_at,
        }
        for job_id, plugin_id, source_path, attempts, error, updated_at in rows
    ]
