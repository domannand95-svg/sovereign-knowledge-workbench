from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone


DECISIONS = {"approved", "rejected", "needs_research", "quarantined"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def install(database: sqlite3.Connection) -> None:
    database.executescript("""
    CREATE TABLE IF NOT EXISTS review_candidates (
      candidate_id TEXT PRIMARY KEY,
      job_id TEXT NOT NULL UNIQUE REFERENCES jobs(job_id),
      result_sha256 TEXT NOT NULL,
      admitted_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS review_decisions (
      decision_id TEXT PRIMARY KEY,
      candidate_id TEXT NOT NULL REFERENCES review_candidates(candidate_id),
      decision TEXT NOT NULL CHECK(decision IN ('approved','rejected','needs_research','quarantined')),
      reviewer TEXT NOT NULL,
      reason TEXT NOT NULL,
      decided_at TEXT NOT NULL,
      previous_decision_id TEXT,
      decision_hash TEXT
    );
    """)
    columns = {row[1] for row in database.execute("PRAGMA table_info(review_decisions)")}
    if "previous_decision_id" not in columns:
        database.execute("ALTER TABLE review_decisions ADD COLUMN previous_decision_id TEXT")
    if "decision_hash" not in columns:
        database.execute("ALTER TABLE review_decisions ADD COLUMN decision_hash TEXT")
    database.executescript("""
    CREATE INDEX IF NOT EXISTS review_decisions_candidate_idx
      ON review_decisions(candidate_id, decided_at);
    CREATE TRIGGER IF NOT EXISTS review_candidates_no_update
      BEFORE UPDATE ON review_candidates BEGIN SELECT RAISE(ABORT, 'review candidates are immutable'); END;
    CREATE TRIGGER IF NOT EXISTS review_candidates_no_delete
      BEFORE DELETE ON review_candidates BEGIN SELECT RAISE(ABORT, 'review candidates are immutable'); END;
    CREATE TRIGGER IF NOT EXISTS review_decisions_no_update
      BEFORE UPDATE ON review_decisions BEGIN SELECT RAISE(ABORT, 'review decisions are immutable'); END;
    CREATE TRIGGER IF NOT EXISTS review_decisions_no_delete
      BEFORE DELETE ON review_decisions BEGIN SELECT RAISE(ABORT, 'review decisions are immutable'); END;
    """)
    database.commit()


def admit_completed(database: sqlite3.Connection, *, limit: int = 100) -> int:
    if limit < 1 or limit > 1000:
        raise ValueError("Review admission limit must be between 1 and 1000")
    install(database)
    rows = database.execute(
        "SELECT j.job_id,j.result_json FROM jobs j WHERE j.status='completed' AND j.result_json IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM review_candidates c WHERE c.job_id=j.job_id) "
        "ORDER BY updated_at,job_id LIMIT ?", (limit,)
    ).fetchall()
    admitted = 0
    for job_id, result_json in rows:
        canonical = json.dumps(json.loads(result_json), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        result_sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        candidate_id = hashlib.sha256(f"{job_id}\0{result_sha256}".encode()).hexdigest()
        cursor = database.execute(
            "INSERT OR IGNORE INTO review_candidates(candidate_id,job_id,result_sha256,admitted_at) VALUES(?,?,?,?)",
            (candidate_id, job_id, result_sha256, _now()),
        )
        admitted += cursor.rowcount
    database.commit()
    return admitted


def list_candidates(database: sqlite3.Connection, *, limit: int = 100) -> list[dict[str, object]]:
    if limit < 1 or limit > 1000:
        raise ValueError("Review query limit must be between 1 and 1000")
    install(database)
    rows = database.execute("""
      SELECT c.candidate_id,c.job_id,j.plugin_id,j.source_path,c.result_sha256,c.admitted_at,
             d.decision,d.reviewer,d.reason,d.decided_at
      FROM review_candidates c JOIN jobs j ON j.job_id=c.job_id
      LEFT JOIN review_decisions d ON d.decision_id=(
        SELECT decision_id FROM review_decisions WHERE candidate_id=c.candidate_id
        ORDER BY decided_at DESC,decision_id DESC LIMIT 1)
      ORDER BY c.admitted_at,c.candidate_id LIMIT ?
    """, (limit,)).fetchall()
    keys = ("candidate_id", "job_id", "plugin_id", "source_path", "result_sha256", "admitted_at",
            "decision", "reviewer", "reason", "decided_at")
    return [dict(zip(keys, row, strict=True)) for row in rows]


def decide(database: sqlite3.Connection, candidate_id: str, decision: str, reviewer: str, reason: str) -> str:
    install(database)
    if decision not in DECISIONS:
        raise ValueError(f"Unknown review decision: {decision}")
    if not reviewer.strip() or not reason.strip():
        raise ValueError("Reviewer and reason are required")
    row = database.execute("""
      SELECT c.job_id,c.result_sha256,j.result_json,j.status
      FROM review_candidates c JOIN jobs j ON j.job_id=c.job_id WHERE c.candidate_id=?
    """, (candidate_id,)).fetchone()
    if row is None:
        raise ValueError("Unknown review candidate")
    job_id, expected_hash, result_json, status = row
    if status != "completed" or result_json is None:
        raise ValueError("Review candidate no longer binds a completed result")
    canonical = json.dumps(json.loads(result_json), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if hashlib.sha256(canonical.encode()).hexdigest() != expected_hash:
        raise ValueError("Review candidate result identity mismatch")
    decided_at = _now()
    previous = database.execute(
        "SELECT decision_id FROM review_decisions WHERE candidate_id=? ORDER BY decided_at DESC,decision_id DESC LIMIT 1",
        (candidate_id,),
    ).fetchone()
    previous_decision_id = previous[0] if previous else ""
    decision_id = _decision_hash(candidate_id, decision, reviewer.strip(), reason.strip(), decided_at, previous_decision_id)
    database.execute(
        "INSERT INTO review_decisions(decision_id,candidate_id,decision,reviewer,reason,decided_at,previous_decision_id,decision_hash) VALUES(?,?,?,?,?,?,?,?)",
        (decision_id, candidate_id, decision, reviewer.strip(), reason.strip(), decided_at,
         previous_decision_id or None, decision_id),
    )
    database.commit()
    return decision_id


def _decision_hash(candidate_id: str, decision: str, reviewer: str, reason: str,
                   decided_at: str, previous_decision_id: str) -> str:
    digest = hashlib.sha256(b"SOVEREIGN_WORKBENCH_REVIEW_DECISION_V1")
    for field in (candidate_id, decision, reviewer, reason, decided_at, previous_decision_id):
        encoded = field.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def verify_decisions(database: sqlite3.Connection) -> dict[str, int | bool]:
    install(database)
    checked = 0
    candidates = database.execute("SELECT candidate_id FROM review_candidates ORDER BY candidate_id").fetchall()
    for (candidate_id,) in candidates:
        previous = ""
        rows = database.execute(
            "SELECT decision_id,decision,reviewer,reason,decided_at,previous_decision_id,decision_hash "
            "FROM review_decisions WHERE candidate_id=? ORDER BY decided_at,decision_id", (candidate_id,)
        ).fetchall()
        for decision_id, decision, reviewer, reason, decided_at, linked_previous, stored_hash in rows:
            expected = _decision_hash(candidate_id, decision, reviewer, reason, decided_at, previous)
            if linked_previous != (previous or None) or decision_id != expected or stored_hash != expected:
                raise ValueError(f"Review decision chain verification failed for {candidate_id}")
            previous = decision_id
            checked += 1
    return {"valid": True, "candidates": len(candidates), "decisions": checked}
