from __future__ import annotations

import hashlib
import json
import sqlite3
import csv
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
CREATE TABLE IF NOT EXISTS archive_review_decisions (
  decision_id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES review_sources(source_id),
  batch_id TEXT NOT NULL REFERENCES review_batches(batch_id),
  decision TEXT NOT NULL CHECK(decision IN ('APPROVE','REJECT','NEEDS_RESEARCH','QUARANTINE')),
  topic TEXT NOT NULL,
  maturity TEXT NOT NULL,
  authority TEXT NOT NULL,
  confidence TEXT NOT NULL CHECK(confidence IN ('HIGH','MEDIUM','LOW','UNKNOWN')),
  privacy_status TEXT NOT NULL,
  canonical_status TEXT NOT NULL,
  review_note TEXT NOT NULL,
  supersedes TEXT NOT NULL,
  replaced_by TEXT NOT NULL,
  research_question TEXT NOT NULL,
  reviewer TEXT NOT NULL,
  decided_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS archive_review_candidates (
  source_id TEXT PRIMARY KEY REFERENCES review_sources(source_id),
  batch_id TEXT NOT NULL REFERENCES review_batches(batch_id),
  model_summary TEXT NOT NULL,
  proposed_topic TEXT NOT NULL,
  proposed_maturity TEXT NOT NULL,
  proposed_authority TEXT NOT NULL,
  proposal_reason TEXT NOT NULL,
  candidate_sha256 TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS review_batches_no_update
  BEFORE UPDATE ON review_batches BEGIN SELECT RAISE(ABORT, 'review batches are immutable'); END;
CREATE TRIGGER IF NOT EXISTS review_batches_no_delete
  BEFORE DELETE ON review_batches BEGIN SELECT RAISE(ABORT, 'review batches are immutable'); END;
CREATE TRIGGER IF NOT EXISTS review_batch_items_no_update
  BEFORE UPDATE ON review_batch_items BEGIN SELECT RAISE(ABORT, 'review batch items are immutable'); END;
CREATE TRIGGER IF NOT EXISTS review_batch_items_no_delete
  BEFORE DELETE ON review_batch_items BEGIN SELECT RAISE(ABORT, 'review batch items are immutable'); END;
CREATE TRIGGER IF NOT EXISTS archive_review_decisions_no_update
  BEFORE UPDATE ON archive_review_decisions BEGIN SELECT RAISE(ABORT, 'archive review decisions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS archive_review_decisions_no_delete
  BEFORE DELETE ON archive_review_decisions BEGIN SELECT RAISE(ABORT, 'archive review decisions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS archive_review_candidates_no_update
  BEFORE UPDATE ON archive_review_candidates BEGIN SELECT RAISE(ABORT, 'archive review candidates are immutable'); END;
CREATE TRIGGER IF NOT EXISTS archive_review_candidates_no_delete
  BEFORE DELETE ON archive_review_candidates BEGIN SELECT RAISE(ABORT, 'archive review candidates are immutable'); END;
"""

TOPICS = {"Governance", "Metabolism", "Energy", "Compute", "Perception", "Other", "Needs review"}
MATURITIES = {"Production", "Normative specification", "Proposed specification", "Research", "Evidence", "Handover", "Archive", "Needs review"}
AUTHORITIES = {"Implemented authority boundary", "Non-authoritative candidate", "No authority", "Needs review"}
DECISIONS = {"APPROVE", "REJECT", "NEEDS_RESEARCH", "QUARANTINE"}
REVIEW_COLUMNS = (
    "batch_id", "source_id", "source_path", "source_sha256", "modified_ns", "extraction_status",
    "model_summary", "proposed_topic", "proposed_maturity", "proposed_authority", "proposal_reason",
    "decision", "topic", "maturity", "authority", "confidence", "privacy_status",
    "canonical_status", "review_note", "supersedes", "replaced_by", "research_question", "reviewer",
)


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


def export_review_csv(database: sqlite3.Connection, batch_id: str, output: Path) -> Path:
    if output.exists():
        raise ValueError("Review export already exists; prior exports are never overwritten")
    rows = database.execute(
        "SELECT s.source_id,s.relative_path,s.source_sha256,s.modified_ns,s.extraction_status,"
        "COALESCE(c.model_summary,''),COALESCE(c.proposed_topic,''),COALESCE(c.proposed_maturity,''),"
        "COALESCE(c.proposed_authority,''),COALESCE(c.proposal_reason,'') "
        "FROM review_batch_items i JOIN review_sources s ON s.source_id=i.source_id "
        "LEFT JOIN archive_review_candidates c ON c.source_id=s.source_id "
        "WHERE i.batch_id=? ORDER BY i.ordinal", (batch_id,),
    ).fetchall()
    if not rows:
        raise ValueError("Unknown or empty review batch")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=REVIEW_COLUMNS)
        writer.writeheader()
        for source_id, path, digest, modified_ns, extraction_status, summary, topic, maturity, authority, reason in rows:
            value = {column: "" for column in REVIEW_COLUMNS}
            value.update({"batch_id": batch_id, "source_id": source_id, "source_path": path,
                          "source_sha256": digest, "modified_ns": modified_ns,
                          "extraction_status": extraction_status, "decision": "NEEDS_RESEARCH",
                          "model_summary": summary, "proposed_topic": topic,
                          "proposed_maturity": maturity, "proposed_authority": authority,
                          "proposal_reason": reason,
                          "topic": "Needs review", "maturity": "Needs review",
                          "authority": "Needs review", "confidence": "UNKNOWN",
                          "privacy_status": "Needs review", "canonical_status": "UNRESOLVED"})
            writer.writerow(value)
    return output


def store_candidates(database: sqlite3.Connection, batch_id: str, candidates: list[dict[str, str]]) -> int:
    stored = 0
    for candidate in candidates:
        bound = database.execute("SELECT 1 FROM review_batch_items WHERE batch_id=? AND source_id=?",
                                 (batch_id, candidate["source_id"])).fetchone()
        if not bound:
            raise ValueError("Candidate is not bound to this batch")
        value = {key: str(candidate.get(key, "")) for key in
                 ("source_id", "model_summary", "proposed_topic", "proposed_maturity",
                  "proposed_authority", "proposal_reason")}
        canonical = json.dumps(value, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        cursor = database.execute(
            "INSERT OR IGNORE INTO archive_review_candidates VALUES(?,?,?,?,?,?,?,?)",
            (value["source_id"], batch_id, value["model_summary"], value["proposed_topic"],
             value["proposed_maturity"], value["proposed_authority"], value["proposal_reason"], digest),
        )
        stored += cursor.rowcount
    database.commit()
    return stored


def import_review_csv(database: sqlite3.Connection, path: Path) -> int:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if tuple(reader.fieldnames or ()) != REVIEW_COLUMNS:
            raise ValueError("Review CSV columns do not match the frozen contract")
        rows = list(reader)
    imported = 0
    database.execute("BEGIN IMMEDIATE")
    try:
        for row in rows:
            if row["decision"] not in DECISIONS or row["topic"] not in TOPICS or row["maturity"] not in MATURITIES or row["authority"] not in AUTHORITIES:
                raise ValueError("Review row contains an invalid controlled value")
            if row["confidence"] not in {"HIGH", "MEDIUM", "LOW", "UNKNOWN"}:
                raise ValueError("Review confidence is invalid")
            if not row["reviewer"].strip() or not row["review_note"].strip():
                raise ValueError("Reviewer and review_note are required")
            if row["decision"] == "NEEDS_RESEARCH" and not row["research_question"].strip():
                raise ValueError("NEEDS_RESEARCH requires a research question")
            bound = database.execute(
                "SELECT s.source_sha256 FROM review_batch_items i JOIN review_sources s ON s.source_id=i.source_id "
                "WHERE i.batch_id=? AND i.source_id=?", (row["batch_id"], row["source_id"]),
            ).fetchone()
            if not bound or bound[0] != row["source_sha256"]:
                raise ValueError("Review row source identity mismatch")
            canonical = json.dumps({key: row[key] for key in REVIEW_COLUMNS}, sort_keys=True, separators=(",", ":"))
            decision_id = hashlib.sha256(("SOVEREIGN_WORKBENCH_ARCHIVE_REVIEW_V1\0" + canonical).encode()).hexdigest()
            cursor = database.execute(
                "INSERT OR IGNORE INTO archive_review_decisions("
                "decision_id,source_id,batch_id,decision,topic,maturity,authority,confidence,privacy_status,"
                "canonical_status,review_note,supersedes,replaced_by,research_question,reviewer,decided_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (decision_id, row["source_id"], row["batch_id"], row["decision"], row["topic"],
                 row["maturity"], row["authority"], row["confidence"], row["privacy_status"],
                 row["canonical_status"], row["review_note"], row["supersedes"], row["replaced_by"],
                 row["research_question"], row["reviewer"].strip(), _now()),
            )
            imported += cursor.rowcount
        database.commit()
        return imported
    except Exception:
        database.rollback()
        raise


def export_research_tickets(database: sqlite3.Connection, output: Path) -> int:
    if output.exists():
        raise ValueError("Research-ticket export already exists")
    rows = database.execute(
        "SELECT decision_id,source_id,topic,research_question,privacy_status,review_note "
        "FROM archive_review_decisions WHERE decision='NEEDS_RESEARCH' ORDER BY decision_id"
    ).fetchall()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8", newline="\n") as stream:
        for decision_id, source_id, topic, question, privacy, note in rows:
            ticket = {"contract_version": "sovereign.workbench.research-ticket.v1",
                      "ticket_id": f"RQ-{decision_id[:16]}", "source_id": source_id,
                      "module_id": topic, "question": question, "review_context": note,
                      "privacy_status": privacy, "status": "DRAFT", "authority": "NONE",
                      "requested_output": {"primary_sources": True, "contradictory_evidence": True,
                                           "uncertainty": True, "direct_source_urls": True}}
            stream.write(json.dumps(ticket, ensure_ascii=False, sort_keys=True) + "\n")
    return len(rows)


def export_staging_manifest(database: sqlite3.Connection, output: Path) -> dict[str, object]:
    if output.exists():
        raise ValueError("Staging manifest already exists")
    rows = database.execute(
        "SELECT d.decision_id,s.root,s.relative_path,s.source_sha256,d.topic,d.maturity "
        "FROM archive_review_decisions d JOIN review_sources s ON s.source_id=d.source_id "
        "WHERE d.decision='APPROVE' ORDER BY d.decision_id"
    ).fetchall()
    items = [{"decision_id": decision_id, "source_path": str(Path(root) / relative_path),
              "source_sha256": digest, "proposed_relative_target": str(Path(topic) / maturity / relative_path),
              "operation": "copy", "status": "PROPOSED", "authority_required": "filesystem.write"}
             for decision_id, root, relative_path, digest, topic, maturity in rows]
    value = {"contract_version": "sovereign.workbench.staging-manifest.v1", "authority": "none",
             "execution_authorized": False, "items": items}
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    value["manifest_sha256"] = hashlib.sha256(canonical.encode()).hexdigest()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    return value


def export_training_split(database: sqlite3.Connection, train: Path, evaluation: Path) -> dict[str, object]:
    if train.exists() or evaluation.exists():
        raise ValueError("Training or evaluation export already exists")
    rows = database.execute(
        "SELECT d.decision_id,d.source_id,d.decision,d.topic,d.maturity,d.authority,d.confidence,"
        "d.privacy_status,d.canonical_status,d.review_note,d.research_question,"
        "COALESCE(c.model_summary,''),COALESCE(c.proposed_topic,''),COALESCE(c.proposed_maturity,''),"
        "COALESCE(c.proposed_authority,''),COALESCE(c.proposal_reason,'') "
        "FROM archive_review_decisions d LEFT JOIN archive_review_candidates c ON c.source_id=d.source_id "
        "ORDER BY d.decision_id"
    ).fetchall()
    if len(rows) < 5:
        raise ValueError("At least five reviewed examples are required for a split")
    train_rows, eval_rows = [], []
    for row in rows:
        (decision_id, source_id, decision, topic, maturity, authority, confidence, privacy,
         canonical, note, question, summary, proposed_topic, proposed_maturity,
         proposed_authority, reason) = row
        example = {"contract_version": "sovereign.workbench.review-example.v1",
                   "example_id": decision_id, "source_id": source_id,
                   "input": {"summary": summary, "proposed_topic": proposed_topic,
                             "proposed_maturity": proposed_maturity,
                             "proposed_authority": proposed_authority, "proposal_reason": reason},
                   "expected": {"decision": decision, "topic": topic, "maturity": maturity,
                                "authority": authority, "confidence": confidence,
                                "privacy_status": privacy, "canonical_status": canonical,
                                "review_note": note, "research_question": question}}
        bucket = int(hashlib.sha256(source_id.encode()).hexdigest()[:8], 16) % 5
        (eval_rows if bucket == 0 else train_rows).append(example)
    if not train_rows or not eval_rows:
        raise ValueError("Deterministic split requires examples spanning both hash buckets")
    for path, values in ((train, train_rows), (evaluation, eval_rows)):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            for value in values:
                stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")
    return {"train_examples": len(train_rows), "evaluation_examples": len(eval_rows),
            "train_sha256": hashlib.sha256(train.read_bytes()).hexdigest(),
            "evaluation_sha256": hashlib.sha256(evaluation.read_bytes()).hexdigest()}
