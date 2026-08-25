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
CREATE TABLE IF NOT EXISTS archive_review_routes (
  source_id TEXT PRIMARY KEY REFERENCES review_sources(source_id),
  batch_id TEXT NOT NULL REFERENCES review_batches(batch_id),
  review_route TEXT NOT NULL CHECK(review_route IN (
    'SPOT_CHECK_ELIGIBLE','MANUAL_REVIEW_REQUIRED','RECOVERY_REQUIRED'
  )),
  route_reasons TEXT NOT NULL,
  classification_confidence REAL NOT NULL CHECK(
    classification_confidence >= 0.0 AND classification_confidence <= 1.0
  ),
  route_sha256 TEXT NOT NULL,
  generated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS archive_review_relationships (
  relationship_id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES review_sources(source_id),
  batch_id TEXT NOT NULL REFERENCES review_batches(batch_id),
  relationship_type TEXT NOT NULL CHECK(relationship_type IN (
    'EXACT_DUPLICATE','POSSIBLE_PARENT','VERSION_SIBLING','POSSIBLE_SUPERSEDES','HASH_COMPANION'
  )),
  related_relative_path TEXT NOT NULL,
  related_sha256 TEXT NOT NULL,
  confidence REAL NOT NULL CHECK(confidence >= 0.0 AND confidence <= 1.0),
  evidence TEXT NOT NULL,
  authority TEXT NOT NULL CHECK(authority='NONE'),
  created_at TEXT NOT NULL
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
CREATE TRIGGER IF NOT EXISTS archive_review_routes_no_update
  BEFORE UPDATE ON archive_review_routes BEGIN SELECT RAISE(ABORT, 'archive review routes are immutable'); END;
CREATE TRIGGER IF NOT EXISTS archive_review_routes_no_delete
  BEFORE DELETE ON archive_review_routes BEGIN SELECT RAISE(ABORT, 'archive review routes are immutable'); END;
CREATE TRIGGER IF NOT EXISTS archive_review_relationships_no_update
  BEFORE UPDATE ON archive_review_relationships BEGIN SELECT RAISE(ABORT, 'archive review relationships are immutable'); END;
CREATE TRIGGER IF NOT EXISTS archive_review_relationships_no_delete
  BEFORE DELETE ON archive_review_relationships BEGIN SELECT RAISE(ABORT, 'archive review relationships are immutable'); END;
"""

TOPICS = {"Governance", "Metabolism", "Energy", "Compute", "Perception", "Other", "Needs review"}
MATURITIES = {"Production", "Normative specification", "Proposed specification", "Research", "Evidence", "Handover", "Archive", "Needs review"}
AUTHORITIES = {"Implemented authority boundary", "Non-authoritative candidate", "No authority", "Needs review"}
DECISIONS = {"APPROVE", "REJECT", "NEEDS_RESEARCH", "QUARANTINE"}
REVIEW_ROUTES = {"SPOT_CHECK_ELIGIBLE", "MANUAL_REVIEW_REQUIRED", "RECOVERY_REQUIRED"}
RELATIONSHIP_TYPES = {"EXACT_DUPLICATE", "POSSIBLE_PARENT", "VERSION_SIBLING",
                      "POSSIBLE_SUPERSEDES", "HASH_COMPANION"}
REVIEW_COLUMNS = (
    "batch_id", "source_id", "source_path", "source_sha256", "modified_ns", "extraction_status",
    "model_summary", "proposed_topic", "proposed_maturity", "proposed_authority", "proposal_reason",
    "classification_confidence", "review_route", "route_reasons", "relationship_candidates",
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


def routing_counts(database: sqlite3.Connection) -> dict[str, int]:
    result = {route: 0 for route in REVIEW_ROUTES}
    for route, count in database.execute(
        "SELECT review_route,COUNT(*) FROM archive_review_routes GROUP BY review_route"
    ):
        result[route] = count
    return result


def _review_rows(database: sqlite3.Connection, batch_id: str) -> list[tuple[object, ...]]:
    rows = database.execute(
        "SELECT s.source_id,s.relative_path,s.source_sha256,s.modified_ns,s.extraction_status,"
        "COALESCE(c.model_summary,''),COALESCE(c.proposed_topic,''),COALESCE(c.proposed_maturity,''),"
        "COALESCE(c.proposed_authority,''),COALESCE(c.proposal_reason,''),"
        "COALESCE(r.classification_confidence,0.0),COALESCE(r.review_route,'MANUAL_REVIEW_REQUIRED'),"
        "COALESCE(r.route_reasons,'candidate routing is unavailable') "
        "FROM review_batch_items i JOIN review_sources s ON s.source_id=i.source_id "
        "LEFT JOIN archive_review_candidates c ON c.source_id=s.source_id "
        "LEFT JOIN archive_review_routes r ON r.source_id=s.source_id "
        "WHERE i.batch_id=? ORDER BY i.ordinal", (batch_id,),
    ).fetchall()
    relationships: dict[str, list[dict[str, object]]] = {}
    for source_id, relation, path, digest, confidence, evidence in database.execute(
        "SELECT source_id,relationship_type,related_relative_path,related_sha256,confidence,evidence "
        "FROM archive_review_relationships WHERE batch_id=? "
        "ORDER BY source_id,relationship_type,related_relative_path", (batch_id,),
    ):
        relationships.setdefault(source_id, []).append({
            "type": relation, "path": path, "sha256": digest,
            "confidence": confidence, "evidence": evidence,
        })
    return [row + (json.dumps(relationships.get(row[0], []), separators=(",", ":")),)
            for row in rows]


def export_review_csv(database: sqlite3.Connection, batch_id: str, output: Path) -> Path:
    if output.exists():
        raise ValueError("Review export already exists; prior exports are never overwritten")
    rows = _review_rows(database, batch_id)
    if not rows:
        raise ValueError("Unknown or empty review batch")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=REVIEW_COLUMNS)
        writer.writeheader()
        for (source_id, path, digest, modified_ns, extraction_status, summary, topic, maturity,
             authority, reason, classification_confidence, review_route, route_reasons,
             relationship_candidates) in rows:
            value = {column: "" for column in REVIEW_COLUMNS}
            value.update({"batch_id": batch_id, "source_id": source_id, "source_path": path,
                          "source_sha256": digest, "modified_ns": modified_ns,
                          "extraction_status": extraction_status,
                          "model_summary": summary, "proposed_topic": topic,
                          "proposed_maturity": maturity, "proposed_authority": authority,
                          "proposal_reason": reason,
                          "classification_confidence": classification_confidence,
                          "review_route": review_route, "route_reasons": route_reasons,
                          "relationship_candidates": relationship_candidates,
                          "topic": "Needs review", "maturity": "Needs review",
                          "authority": "Needs review", "confidence": "UNKNOWN",
                          "privacy_status": "Needs review", "canonical_status": "UNRESOLVED"})
            writer.writerow(value)
    return output


def export_review_xlsx(database: sqlite3.Connection, batch_id: str, output: Path) -> Path:
    if output.exists():
        raise ValueError("Review workbook already exists; prior exports are never overwritten")
    try:
        import xlsxwriter
    except ImportError as exc:
        raise ValueError("Excel export requires installation with the 'excel' extra") from exc
    rows = _review_rows(database, batch_id)
    if not rows:
        raise ValueError("Unknown or empty review batch")
    output.parent.mkdir(parents=True, exist_ok=True)
    workbook = xlsxwriter.Workbook(str(output), {"constant_memory": True})
    try:
        sheet = workbook.add_worksheet("Review Ledger")
        header = workbook.add_format({"bold": True, "font_color": "white", "bg_color": "#17365D",
                                      "text_wrap": True, "valign": "top"})
        wrap = workbook.add_format({"text_wrap": True, "valign": "top"})
        sheet.hide_gridlines(2); sheet.freeze_panes(1, 1); sheet.autofilter(0, 0, len(rows), len(REVIEW_COLUMNS)-1)
        for column, name in enumerate(REVIEW_COLUMNS): sheet.write(0, column, name, header)
        defaults = {"decision": "", "topic": "Needs review", "maturity": "Needs review",
                    "authority": "Needs review", "confidence": "UNKNOWN", "privacy_status": "Needs review",
                    "canonical_status": "UNRESOLVED"}
        for row_number, row in enumerate(rows, start=1):
            (source_id, path, digest, modified_ns, extraction, summary, topic, maturity, authority,
             reason, classification_confidence, review_route, route_reasons,
             relationship_candidates) = row
            value = {column: "" for column in REVIEW_COLUMNS}
            value.update(defaults); value.update({"batch_id": batch_id, "source_id": source_id,
                "source_path": path, "source_sha256": digest, "modified_ns": modified_ns,
                "extraction_status": extraction, "model_summary": summary, "proposed_topic": topic,
                "proposed_maturity": maturity, "proposed_authority": authority, "proposal_reason": reason,
                "classification_confidence": classification_confidence,
                "review_route": review_route, "route_reasons": route_reasons,
                "relationship_candidates": relationship_candidates})
            for column, name in enumerate(REVIEW_COLUMNS): sheet.write(row_number, column, value[name], wrap)
        sheet.set_column(0, 1, 18); sheet.set_column(2, 2, 52); sheet.set_column(3, 4, 18)
        sheet.set_column(5, len(REVIEW_COLUMNS)-1, 20)
        last_row = len(rows)
        for name, values in (("decision", sorted(DECISIONS)), ("topic", sorted(TOPICS)),
                             ("maturity", sorted(MATURITIES)), ("authority", sorted(AUTHORITIES)),
                             ("confidence", ["HIGH", "MEDIUM", "LOW", "UNKNOWN"])):
            column = REVIEW_COLUMNS.index(name)
            sheet.data_validation(1, column, last_row, column,
                                  {"validate": "list", "source": values})
        queue_sheet = workbook.add_worksheet("Review Queue")
        queue_sheet.hide_gridlines(2); queue_sheet.freeze_panes(1, 1)
        queue_columns = ("source_path", "model_summary", "proposed_topic", "proposed_maturity",
                         "classification_confidence", "review_route", "route_reasons",
                         "relationship_candidates", "selection_basis", "decision", "review_note", "reviewer")
        for column, name in enumerate(queue_columns):
            queue_sheet.write(0, column, name, header)
        spot_source = next((row[0] for row in rows if row[11] == "SPOT_CHECK_ELIGIBLE"), None)
        queue_rows = [row for row in rows if row[11] != "SPOT_CHECK_ELIGIBLE" or row[0] == spot_source]
        for row_number, row in enumerate(queue_rows, start=1):
            (source_id, path, _digest, _modified_ns, _extraction, summary, topic, maturity,
             _authority, _reason, classification_confidence, review_route, route_reasons,
             relationship_candidates) = row
            selection_basis = ("QUALITY_CONTROL_SAMPLE" if source_id == spot_source and
                               review_route == "SPOT_CHECK_ELIGIBLE" else "EXCEPTION")
            values = (path, summary, topic, maturity, classification_confidence, review_route,
                      route_reasons, relationship_candidates, selection_basis, "", "", "")
            for column, value in enumerate(values):
                queue_sheet.write(row_number, column, value, wrap)
        queue_sheet.autofilter(0, 0, len(queue_rows), len(queue_columns) - 1)
        queue_sheet.set_column(0, 0, 46); queue_sheet.set_column(1, 1, 58)
        queue_sheet.set_column(2, 3, 24); queue_sheet.set_column(4, 4, 14)
        queue_sheet.set_column(5, 8, 28); queue_sheet.set_column(9, 11, 22)
        if queue_rows:
            decision_column = queue_columns.index("decision")
            queue_sheet.data_validation(1, decision_column, len(queue_rows), decision_column,
                                        {"validate": "list", "source": sorted(DECISIONS)})
            route_column = queue_columns.index("review_route")
            queue_sheet.conditional_format(1, route_column, len(queue_rows), route_column,
                {"type": "text", "criteria": "containing", "value": "RECOVERY_REQUIRED",
                 "format": workbook.add_format({"bg_color": "#F4CCCC", "font_color": "#9C0006"})})
            queue_sheet.conditional_format(1, route_column, len(queue_rows), route_column,
                {"type": "text", "criteria": "containing", "value": "MANUAL_REVIEW_REQUIRED",
                 "format": workbook.add_format({"bg_color": "#FFF2CC", "font_color": "#7F6000"})})
            queue_sheet.conditional_format(1, route_column, len(queue_rows), route_column,
                {"type": "text", "criteria": "containing", "value": "SPOT_CHECK_ELIGIBLE",
                 "format": workbook.add_format({"bg_color": "#D9EAD3", "font_color": "#274E13"})})
        summary_sheet = workbook.add_worksheet("Instructions")
        summary_sheet.hide_gridlines(2); summary_sheet.set_column("A:A", 28); summary_sheet.set_column("B:B", 80)
        summary_sheet.write("A1", "Control", header); summary_sheet.write("B1", "Meaning", header)
        instructions = [("Authority", "Model and workbook outputs are candidates only."),
                        ("Start here", "Use Review Queue instead of reading every row in the complete Review Ledger."),
                        ("Routing", "Review RECOVERY_REQUIRED and MANUAL_REVIEW_REQUIRED rows; use SPOT_CHECK_ELIGIBLE only for a small quality-control sample."),
                        ("Review", "Leave decision blank when no human decision is required. Complete decision fields only for selected rows."),
                        ("Research", "NEEDS_RESEARCH requires a precise research question before import."),
                        ("Files", "No source file is moved, renamed, deleted, or published by this workbook.")]
        for index, pair in enumerate(instructions, start=1):
            summary_sheet.write_row(index, 0, pair, wrap)
    finally:
        workbook.close()
    return output


def _route_candidate(extraction_status: str, candidate: dict[str, object]) -> tuple[str, list[str], float]:
    try:
        confidence = float(candidate.get("classification_confidence", 0.0))
    except (TypeError, ValueError) as exc:
        raise ValueError("Classification confidence must be a number") from exc
    if not 0.0 <= confidence <= 1.0:
        raise ValueError("Classification confidence must be between zero and one")

    if extraction_status != "extracted":
        return "RECOVERY_REQUIRED", [f"extraction_status={extraction_status}"], confidence

    reasons: list[str] = []
    if str(candidate.get("classification_abstained", "false")).casefold() == "true":
        reasons.append("classifier abstained or selected the fallback module")
    if confidence < 0.75:
        reasons.append("classification confidence is below 0.75")
    if not str(candidate.get("model_summary", "")).strip():
        reasons.append("summary is empty")
    if str(candidate.get("proposed_maturity", "")).casefold() in {"", "unknown", "ambiguous"}:
        reasons.append("maturity is unresolved")
    risk_flags = candidate.get("risk_flags", [])
    if isinstance(risk_flags, str):
        try:
            risk_flags = json.loads(risk_flags)
        except json.JSONDecodeError as exc:
            raise ValueError("risk_flags must be a JSON array or list") from exc
    if not isinstance(risk_flags, list) or not all(isinstance(flag, str) for flag in risk_flags):
        raise ValueError("risk_flags must be a JSON array or list of strings")
    high_risk_flags = sorted(set(risk_flags) & {
        "payment_card_candidate", "australian_tax_file_number_candidate"
    })
    disclosure_flags = sorted(set(risk_flags) - set(high_risk_flags))
    if high_risk_flags:
        reasons.append("high-risk privacy candidate: " + ", ".join(high_risk_flags))
    if reasons:
        return "MANUAL_REVIEW_REQUIRED", reasons, confidence
    spot_reasons = ["no deterministic review exception matched"]
    if disclosure_flags:
        spot_reasons.append("external disclosure remains blocked: " + ", ".join(disclosure_flags))
    return "SPOT_CHECK_ELIGIBLE", spot_reasons, confidence


def store_candidates(database: sqlite3.Connection, batch_id: str,
                     candidates: list[dict[str, object]]) -> int:
    stored = 0
    for candidate in candidates:
        bound = database.execute(
            "SELECT s.extraction_status FROM review_batch_items i "
            "JOIN review_sources s ON s.source_id=i.source_id "
            "WHERE i.batch_id=? AND i.source_id=?", (batch_id, candidate["source_id"]),
        ).fetchone()
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
        route, reasons, confidence = _route_candidate(bound[0], candidate)
        route_value = {"source_id": value["source_id"], "batch_id": batch_id,
                       "candidate_sha256": digest, "review_route": route,
                       "route_reasons": reasons, "classification_confidence": confidence}
        route_canonical = json.dumps(route_value, sort_keys=True, separators=(",", ":"))
        route_digest = hashlib.sha256(route_canonical.encode()).hexdigest()
        database.execute(
            "INSERT OR IGNORE INTO archive_review_routes VALUES(?,?,?,?,?,?,?)",
            (value["source_id"], batch_id, route, json.dumps(reasons, separators=(",", ":")),
             confidence, route_digest, _now()),
        )
    database.commit()
    return stored


def store_relationships(database: sqlite3.Connection, batch_id: str,
                        relationships: list[dict[str, object]]) -> int:
    stored = 0
    for relationship in relationships:
        source_id = str(relationship.get("source_id", ""))
        bound = database.execute(
            "SELECT 1 FROM review_batch_items WHERE batch_id=? AND source_id=?",
            (batch_id, source_id),
        ).fetchone()
        if not bound:
            raise ValueError("Relationship source is not bound to this batch")
        relationship_type = str(relationship.get("relationship_type", ""))
        if relationship_type not in RELATIONSHIP_TYPES:
            raise ValueError("Relationship type is invalid")
        related_path = str(relationship.get("related_relative_path", ""))
        related_sha256 = str(relationship.get("related_sha256", ""))
        evidence = str(relationship.get("evidence", ""))
        confidence = float(relationship.get("confidence", 0.0))
        if not related_path or len(related_sha256) != 64 or not evidence:
            raise ValueError("Relationship evidence is incomplete")
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("Relationship confidence is invalid")
        value = {"source_id": source_id, "batch_id": batch_id,
                 "relationship_type": relationship_type,
                 "related_relative_path": related_path,
                 "related_sha256": related_sha256, "confidence": confidence,
                 "evidence": evidence, "authority": "NONE"}
        canonical = json.dumps(value, sort_keys=True, separators=(",", ":"))
        relationship_id = hashlib.sha256(
            ("SOVEREIGN_WORKBENCH_RELATIONSHIP_V1\0" + canonical).encode()
        ).hexdigest()
        cursor = database.execute(
            "INSERT OR IGNORE INTO archive_review_relationships VALUES(?,?,?,?,?,?,?,?,?,?)",
            (relationship_id, source_id, batch_id, relationship_type, related_path,
             related_sha256, confidence, evidence, "NONE", _now()),
        )
        stored += cursor.rowcount
    database.commit()
    return stored


def relationship_counts(database: sqlite3.Connection) -> dict[str, int]:
    result = {relationship_type: 0 for relationship_type in RELATIONSHIP_TYPES}
    for relationship_type, count in database.execute(
        "SELECT relationship_type,COUNT(*) FROM archive_review_relationships "
        "GROUP BY relationship_type"
    ):
        result[relationship_type] = count
    return result


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
            bound = database.execute(
                "SELECT s.source_sha256 FROM review_batch_items i JOIN review_sources s ON s.source_id=i.source_id "
                "WHERE i.batch_id=? AND i.source_id=?", (row["batch_id"], row["source_id"]),
            ).fetchone()
            if not bound or bound[0] != row["source_sha256"]:
                raise ValueError("Review row source identity mismatch")
            if not row["decision"].strip():
                continue
            if row["decision"] not in DECISIONS or row["topic"] not in TOPICS or row["maturity"] not in MATURITIES or row["authority"] not in AUTHORITIES:
                raise ValueError("Review row contains an invalid controlled value")
            if row["confidence"] not in {"HIGH", "MEDIUM", "LOW", "UNKNOWN"}:
                raise ValueError("Review confidence is invalid")
            if not row["reviewer"].strip() or not row["review_note"].strip():
                raise ValueError("Reviewer and review_note are required")
            if row["decision"] == "NEEDS_RESEARCH" and not row["research_question"].strip():
                raise ValueError("NEEDS_RESEARCH requires a research question")
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
    for index, row in enumerate(sorted(rows, key=lambda value: value[1])):
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
        (eval_rows if index % 5 == 0 else train_rows).append(example)
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
