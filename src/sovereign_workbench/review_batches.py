from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .model import FileRecord


SCHEMA = """
CREATE TABLE IF NOT EXISTS review_sources (
  source_id TEXT PRIMARY KEY,
  root TEXT NOT NULL,
  relative_path TEXT NOT NULL,
  source_sha256 TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  modified_ns INTEGER NOT NULL,
  extraction_status TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('pending','batched','reviewed','quarantined')),
  admitted_at TEXT NOT NULL,
  UNIQUE(root, relative_path, source_sha256)
);
CREATE INDEX IF NOT EXISTS review_sources_state_idx
  ON review_sources(state, root, relative_path, source_id);
CREATE TABLE IF NOT EXISTS review_batches (
  batch_id TEXT PRIMARY KEY,
  sequence INTEGER NOT NULL UNIQUE,
  item_count INTEGER NOT NULL,
  manifest_sha256 TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS review_batch_items (
  batch_id TEXT NOT NULL REFERENCES review_batches(batch_id),
  ordinal INTEGER NOT NULL,
  source_id TEXT NOT NULL UNIQUE REFERENCES review_sources(source_id),
  PRIMARY KEY(batch_id, ordinal)
);
CREATE TRIGGER IF NOT EXISTS review_batches_no_update
  BEFORE UPDATE ON review_batches BEGIN SELECT RAISE(ABORT, 'review batches are immutable'); END;
CREATE TRIGGER IF NOT EXISTS review_batches_no_delete
  BEFORE DELETE ON review_batches BEGIN SELECT RAISE(ABORT, 'review batches are immutable'); END;
CREATE TRIGGER IF NOT EXISTS review_batch_items_no_update
  BEFORE UPDATE ON review_batch_items BEGIN SELECT RAISE(ABORT, 'review batch items are immutable'); END;
CREATE TRIGGER IF NOT EXISTS review_batch_items_no_delete
  BEFORE DELETE ON review_batch_items BEGIN SELECT RAISE(ABORT, 'review batch items are immutable'); END;
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    database = sqlite3.connect(path)
    database.execute("PRAGMA foreign_keys=ON")
    database.execute("PRAGMA journal_mode=WAL")
    database.executescript(SCHEMA)
    return database


def _source_id(root: str, record: FileRecord) -> str:
    digest = hashlib.sha256(b"SOVEREIGN_WORKBENCH_REVIEW_SOURCE_V1")
    for value in (root, record.relative_path, record.sha256):
        encoded = value.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def admit(database: sqlite3.Connection, root: Path, records: list[FileRecord]) -> int:
    canonical_root = str(root.resolve(strict=True))
    admitted = 0
    for record in records:
        if not record.sha256:
            continue
        source_id = _source_id(canonical_root, record)
        cursor = database.execute(
            "INSERT OR IGNORE INTO review_sources("
            "source_id,root,relative_path,source_sha256,size_bytes,modified_ns,extraction_status,state,admitted_at"
            ") VALUES(?,?,?,?,?,?,?,?,?)",
            (source_id, canonical_root, record.relative_path, record.sha256, record.size_bytes,
             record.modified_ns, record.extraction_status, "pending", _now()),
        )
        admitted += cursor.rowcount
    database.commit()
    return admitted


def create_next(database: sqlite3.Connection, *, limit: int = 25) -> dict[str, object] | None:
    if limit < 1 or limit > 1000:
        raise ValueError("Batch limit must be between 1 and 1000")
    database.execute("BEGIN IMMEDIATE")
    try:
        rows = database.execute(
            "SELECT source_id,root,relative_path,source_sha256,size_bytes,modified_ns,extraction_status "
            "FROM review_sources WHERE state='pending' ORDER BY root,relative_path,source_id LIMIT ?",
            (limit,),
        ).fetchall()
        if not rows:
            database.rollback()
            return None
        sequence = database.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM review_batches").fetchone()[0]
        items = [dict(zip(("source_id", "root", "relative_path", "source_sha256", "size_bytes",
                           "modified_ns", "extraction_status"), row, strict=True)) for row in rows]
        manifest = {"contract_version": "sovereign.workbench.review-batch.v1",
                    "sequence": sequence, "authority": "none", "items": items}
        canonical = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        manifest_sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        batch_id = hashlib.sha256(f"SOVEREIGN_WORKBENCH_REVIEW_BATCH_V1\0{sequence}\0{manifest_sha256}".encode()).hexdigest()
        database.execute(
            "INSERT INTO review_batches(batch_id,sequence,item_count,manifest_sha256,created_at) VALUES(?,?,?,?,?)",
            (batch_id, sequence, len(items), manifest_sha256, _now()),
        )
        for ordinal, item in enumerate(items, start=1):
            database.execute("INSERT INTO review_batch_items(batch_id,ordinal,source_id) VALUES(?,?,?)",
                             (batch_id, ordinal, item["source_id"]))
            database.execute("UPDATE review_sources SET state='batched' WHERE source_id=? AND state='pending'",
                             (item["source_id"],))
        database.commit()
        return {"batch_id": batch_id, "manifest_sha256": manifest_sha256, **manifest}
    except Exception:
        database.rollback()
        raise


def counts(database: sqlite3.Connection) -> dict[str, int]:
    result = {state: 0 for state in ("pending", "batched", "reviewed", "quarantined")}
    for state, count in database.execute("SELECT state,COUNT(*) FROM review_sources GROUP BY state"):
        result[state] = count
    result["batches"] = database.execute("SELECT COUNT(*) FROM review_batches").fetchone()[0]
    return result
