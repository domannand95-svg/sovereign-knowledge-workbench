from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone

from .family_review import _verified_text


TOPIC_TERMS = {
    "Governance": {"authority", "authorization", "canonical", "capability", "consensus",
                   "execution", "governance", "policy", "quorum", "sovereign"},
    "Metabolism": {"biological", "circular", "compost", "metabolic", "organics", "recovery",
                   "recycling", "waste"},
    "Energy": {"energy", "fusion", "magnetic", "mhd", "plasma", "power", "reactor", "thermal"},
    "Compute": {"ai", "compute", "neuromorphic", "photonic", "processor", "quantum", "ternary"},
    "Perception": {"acoustic", "isac", "mesh", "observatory", "sensor", "spatial", "telemetry"},
    "Culture": {"art", "community", "discord", "mysticism", "ora", "spiritual"},
}
WORDS = re.compile(r"[a-z0-9]+")
NUMBERED_HEADING = re.compile(r"^(?:appendix|annex|addendum|section|chapter|part)?\s*[a-z0-9ivx]+[.):-]\s+", re.I)
SHA256_AT_END = re.compile(r"\b[0-9a-f]{64}\s*$", re.I)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical(value: dict[str, object]) -> tuple[str, str]:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _heading(line: str) -> bool:
    stripped = line.strip()
    if not stripped or len(stripped) > 180:
        return False
    words = stripped.split()
    return (stripped.startswith("#") or bool(NUMBERED_HEADING.match(stripped))
            or stripped.endswith(":") or (len(words) <= 14 and stripped.isupper()))


def split_sections(text: str, *, target_chars: int = 2_500) -> list[tuple[str, str]]:
    """Deterministically segment plain extracted text without changing the source document."""
    sections: list[tuple[str, str]] = []
    heading = "Document opening"
    body: list[str] = []

    def flush() -> None:
        nonlocal body
        content = "\n".join(body).strip()
        if content:
            sections.append((heading, content))
        body = []

    for line in text.splitlines():
        stripped = line.strip()
        if _heading(stripped):
            flush(); heading = stripped.lstrip("# ").rstrip(":") or heading
        elif stripped:
            body.append(stripped)
            if sum(len(item) + 1 for item in body) >= target_chars:
                flush(); heading = f"{heading} (continued)"
    flush()
    if not sections and text.strip():
        sections.append(("Document body", text.strip()))
    return sections


def _topic_scores(heading: str, content: str) -> list[tuple[str, int]]:
    tokens = set(WORDS.findall(f"{heading} {content}".casefold()))
    return sorted(((topic, len(tokens & terms)) for topic, terms in TOPIC_TERMS.items()),
                  key=lambda item: (-item[1], item[0]))


def _is_provenance_companion(relative_path: str, excerpts: list[str]) -> bool:
    """Identify bounded checksum companions that carry provenance, not reviewable claims."""
    combined = " ".join(excerpts).strip()
    return ("hash" in relative_path.casefold()
            and len(combined) <= 300
            and bool(SHA256_AT_END.search(combined)))


def build_section_proposals(database: sqlite3.Connection) -> dict[str, int]:
    rows = database.execute(
        "SELECT s.source_id,s.root,s.relative_path,s.source_sha256,"
        "COALESCE(c.proposed_topic,'Needs review') FROM review_sources s "
        "LEFT JOIN archive_review_candidates c ON c.source_id=s.source_id "
        "ORDER BY s.relative_path,s.source_id"
    ).fetchall()
    counts = {"sections": 0, "proposals": 0, "cross_family_sections": 0}
    for source_id, root, relative_path, source_hash, default_topic in rows:
        text = _verified_text(root, relative_path, source_hash)
        for ordinal, (heading, content) in enumerate(split_sections(text), start=1):
            content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            section_value = {"source_id": source_id, "source_sha256": source_hash,
                             "ordinal": ordinal, "heading": heading,
                             "content_sha256": content_hash, "authority": "NONE"}
            _, section_hash = _canonical(section_value); section_id = f"SEC-{section_hash[:24]}"
            counts["sections"] += database.execute(
                "INSERT OR IGNORE INTO archive_document_sections VALUES(?,?,?,?,?,?,?,?,?,?)",
                (section_id, source_id, source_hash, ordinal, heading, content[:600], content_hash,
                 section_hash, "NONE", _now())).rowcount
            scored = [(topic, score) for topic, score in _topic_scores(heading, content) if score >= 2]
            if not scored:
                scored = [(default_topic, 0)]
            maximum = max(score for _, score in scored)
            selected = [(topic, score) for topic, score in scored
                        if score == maximum or (maximum >= 4 and score >= maximum - 1)]
            if len(selected) > 1:
                counts["cross_family_sections"] += 1
            for topic, score in selected:
                family_key = f"topic:{topic.casefold().replace(' ', '-')}"
                confidence = 0.35 if score == 0 else min(0.95, 0.45 + score * 0.08)
                reason = ("Inherited file-level topic because no section vocabulary threshold passed"
                          if score == 0 else f"Matched {score} controlled section-topic terms")
                proposal_value = {"section_id": section_id, "proposed_family_key": family_key,
                                  "proposed_topic": topic, "confidence": round(confidence, 4),
                                  "reason": reason, "authority": "NONE"}
                _, proposal_hash = _canonical(proposal_value); proposal_id = f"SFP-{proposal_hash[:24]}"
                counts["proposals"] += database.execute(
                    "INSERT OR IGNORE INTO archive_section_family_proposals VALUES(?,?,?,?,?,?,?,?,?)",
                    (proposal_id, section_id, family_key, topic, confidence, reason,
                     proposal_hash, "NONE", _now())).rowcount
    database.commit()
    return counts


def section_review_queue(database: sqlite3.Connection) -> list[dict[str, object]]:
    rows = database.execute(
        "SELECT p.proposal_id,p.proposal_sha256,p.proposed_family_key,p.proposed_topic,p.confidence,"
        "p.reason,x.section_id,x.heading,x.bounded_excerpt,s.relative_path,s.source_sha256 "
        "FROM archive_section_family_proposals p JOIN archive_document_sections x "
        "ON x.section_id=p.section_id JOIN review_sources s ON s.source_id=x.source_id "
        "LEFT JOIN archive_section_boundary_decisions d ON d.proposal_id=p.proposal_id "
        "WHERE d.decision_id IS NULL ORDER BY s.relative_path,x.ordinal,p.proposed_family_key"
    ).fetchall()
    return [{"proposal_id": row[0], "proposal_sha256": row[1], "proposed_family_key": row[2],
             "proposed_topic": row[3], "confidence": row[4], "reason": row[5],
             "section_id": row[6], "heading": row[7], "bounded_excerpt": row[8],
             "relative_path": row[9], "source_sha256": row[10], "authority": "NONE"}
            for row in rows]


def document_exception_maps(database: sqlite3.Connection) -> list[dict[str, object]]:
    """Collapse section evidence into document maps; retain only material review exceptions."""
    sources = database.execute(
        "SELECT source_id,relative_path,source_sha256 FROM review_sources ORDER BY relative_path"
    ).fetchall()
    maps: list[dict[str, object]] = []
    for source_id, relative_path, source_sha256 in sources:
        candidate = database.execute(
            "SELECT model_summary,proposed_topic,proposed_maturity,proposed_authority,proposal_reason "
            "FROM archive_review_candidates WHERE source_id=?", (source_id,)
        ).fetchone()
        evidence_rows = database.execute(
            "SELECT x.heading,x.bounded_excerpt,p.proposed_topic,p.confidence,p.reason "
            "FROM archive_document_sections x LEFT JOIN archive_section_family_proposals p "
            "ON p.section_id=x.section_id WHERE x.source_id=? "
            "ORDER BY x.ordinal,p.proposed_topic", (source_id,)
        ).fetchall()
        exclusion_rows = database.execute(
            "SELECT DISTINCT e.marker_code,x.heading,e.reason FROM archive_section_exclusions e "
            "JOIN archive_document_sections x ON x.section_id=e.section_id "
            "WHERE x.source_id=? ORDER BY e.marker_code,x.ordinal", (source_id,)
        ).fetchall()
        directive_rows = database.execute(
            "SELECT DISTINCT d.concept_code,d.disposition,x.heading,d.reason "
            "FROM archive_section_directives d JOIN archive_document_sections x "
            "ON x.section_id=d.section_id WHERE x.source_id=? "
            "ORDER BY d.disposition,d.concept_code,x.ordinal", (source_id,)
        ).fetchall()
        topic_rows = database.execute(
            "SELECT p.proposed_topic,COUNT(DISTINCT p.section_id) FROM archive_section_family_proposals p "
            "JOIN archive_document_sections x ON x.section_id=p.section_id WHERE x.source_id=? "
            "GROUP BY p.proposed_topic ORDER BY COUNT(DISTINCT p.section_id) DESC,p.proposed_topic",
            (source_id,)).fetchall()
        cross_rows = database.execute(
            "SELECT x.section_id,x.heading,GROUP_CONCAT(p.proposed_topic),COUNT(*) "
            "FROM archive_document_sections x JOIN archive_section_family_proposals p "
            "ON p.section_id=x.section_id WHERE x.source_id=? GROUP BY x.section_id,x.heading "
            "HAVING COUNT(*)>1 ORDER BY x.ordinal", (source_id,)).fetchall()
        low_rows = database.execute(
            "SELECT p.proposal_id,x.heading,p.proposed_topic,p.confidence,p.reason "
            "FROM archive_section_family_proposals p JOIN archive_document_sections x "
            "ON x.section_id=p.section_id WHERE x.source_id=? AND p.confidence<0.55 "
            "ORDER BY x.ordinal,p.proposed_topic", (source_id,)).fetchall()
        relationship_rows = database.execute(
            "SELECT r.proposal_id,r.relationship_type,r.reason FROM archive_family_relationship_proposals r "
            "LEFT JOIN archive_family_boundary_decisions d ON d.subject_id=r.proposal_id "
            "WHERE (r.source_id=? OR r.related_source_id=?) AND d.decision_id IS NULL "
            "ORDER BY r.proposal_id", (source_id, source_id)).fetchall()
        contradiction_count = database.execute(
            "SELECT COUNT(DISTINCT c.claim_id) FROM archive_family_claims c "
            "JOIN archive_family_claim_coverage v ON v.claim_id=c.claim_id "
            "WHERE v.source_id=? AND EXISTS (SELECT 1 FROM archive_family_claim_coverage other "
            "WHERE other.claim_id=c.claim_id AND other.stance<>v.stance)", (source_id,)).fetchone()[0]
        section_count = database.execute(
            "SELECT COUNT(*) FROM archive_document_sections WHERE source_id=?", (source_id,)
        ).fetchone()[0]
        excluded_section_count = database.execute(
            "SELECT COUNT(DISTINCT x.section_id) FROM archive_document_sections x "
            "JOIN archive_section_exclusions e ON e.section_id=x.section_id WHERE x.source_id=?",
            (source_id,)
        ).fetchone()[0]
        repository_route_count = database.execute(
            "SELECT COUNT(*) FROM archive_repository_artifact_routes WHERE source_id=?",
            (source_id,)
        ).fetchone()[0]
        exception_count = len(cross_rows) + len(low_rows) + len(relationship_rows) + contradiction_count
        value = {
            "source_id": source_id, "relative_path": relative_path, "source_sha256": source_sha256,
            "candidate": {
                "summary": candidate[0], "topic": candidate[1], "maturity": candidate[2],
                "authority": candidate[3], "reason": candidate[4],
            } if candidate else None,
            "section_evidence": [
                {"heading": row[0], "excerpt": row[1], "topic": row[2] or "Unclassified",
                 "confidence": row[3], "reason": row[4] or "No section proposal exists"}
                for row in evidence_rows[:8]
            ],
            "exclusion_warnings": [
                {"marker": row[0], "heading": row[1], "reason": row[2]}
                for row in exclusion_rows
            ],
            "concept_directives": [
                {"concept": row[0], "disposition": row[1],
                 "heading": row[2], "reason": row[3]}
                for row in directive_rows
            ],
            "artifact_role": ("PROVENANCE_COMPANION" if _is_provenance_companion(
                relative_path, [row[1] for row in evidence_rows]
            ) else "REPOSITORY_ARTIFACT" if repository_route_count > 0
                else "EXCLUDED_CONTENT" if section_count > 0 and excluded_section_count == section_count
                else "REVIEWABLE_CONTENT"),
            "topic_coverage": {topic: count for topic, count in topic_rows},
            "section_count": section_count,
            "exception_count": exception_count,
            "exceptions": {
                "cross_family": [{"section_id": row[0], "heading": row[1],
                                  "topics": sorted(set(row[2].split(",")))} for row in cross_rows],
                "low_confidence": {"count": len(low_rows), "examples": [
                    {"proposal_id": row[0], "heading": row[1], "topic": row[2],
                     "confidence": row[3], "reason": row[4]} for row in low_rows[:5]]},
                "unresolved_relationships": [{"proposal_id": row[0], "type": row[1],
                                              "reason": row[2]} for row in relationship_rows],
                "contradictory_claim_count": contradiction_count,
            },
            "review_required": exception_count > 0, "authority": "NONE",
        }
        hash_input = {key: value[key] for key in sorted(value) if key != "authority"}
        _, value["map_sha256"] = _canonical(hash_input)
        maps.append(value)
    return maps


def exception_queue(database: sqlite3.Connection) -> list[dict[str, object]]:
    decided = {row[0] for row in database.execute(
        "SELECT source_id FROM archive_document_map_decisions"
    ).fetchall()}
    return [value for value in document_exception_maps(database)
            if (value["review_required"] and value["source_id"] not in decided
                and value["artifact_role"] == "REVIEWABLE_CONTENT")]


def decide_document_map(database: sqlite3.Connection, source_id: str, source_sha256: str,
                        map_sha256: str, decision: str, reviewer: str, reason: str) -> str:
    if decision not in {"MAP_ACCEPTED", "NEEDS_RESEARCH", "RECLASSIFY"}:
        raise ValueError("Invalid document-map decision")
    if not reviewer.strip() or not reason.strip():
        raise ValueError("Reviewer and reason are required")
    current = next((value for value in document_exception_maps(database)
                    if value["source_id"] == source_id), None)
    if (not current or current["source_sha256"] != source_sha256
            or current["map_sha256"] != map_sha256):
        raise ValueError("Document map or source hash changed; reload before deciding")
    source = database.execute(
        "SELECT root,relative_path FROM review_sources WHERE source_id=?", (source_id,)
    ).fetchone()
    if not source:
        raise ValueError("Document map source is missing")
    _verified_text(source[0], source[1], source_sha256)
    value = {"source_id": source_id, "source_sha256": source_sha256,
             "map_sha256": map_sha256, "decision": decision,
             "reviewer": reviewer.strip(), "reason": reason.strip()}
    _, digest = _canonical(value); decision_id = f"DMD-{digest[:24]}"
    database.execute("INSERT OR IGNORE INTO archive_document_map_decisions VALUES(?,?,?,?,?,?,?,?,?)",
                     (decision_id, source_id, source_sha256, map_sha256, decision,
                      reviewer.strip(), reason.strip(), digest, _now()))
    database.commit(); return decision_id


def decide_section_boundary(database: sqlite3.Connection, proposal_id: str, proposal_sha256: str,
                            decision: str, reviewer: str, reason: str) -> str:
    if decision not in {"APPROVE", "REJECT", "NEEDS_RESEARCH"}:
        raise ValueError("Invalid section-boundary decision")
    if not reviewer.strip() or not reason.strip():
        raise ValueError("Reviewer and reason are required")
    proposal = database.execute(
        "SELECT proposal_sha256 FROM archive_section_family_proposals WHERE proposal_id=?",
        (proposal_id,)).fetchone()
    if not proposal or proposal[0] != proposal_sha256:
        raise ValueError("Section-family proposal hash mismatch")
    value = {"proposal_id": proposal_id, "proposal_sha256": proposal_sha256,
             "decision": decision, "reviewer": reviewer.strip(), "reason": reason.strip()}
    _, digest = _canonical(value); decision_id = f"SBD-{digest[:24]}"
    database.execute("INSERT OR IGNORE INTO archive_section_boundary_decisions VALUES(?,?,?,?,?,?,?,?)",
                     (decision_id, proposal_id, proposal_sha256, decision, reviewer.strip(),
                      reason.strip(), digest, _now()))
    database.commit(); return decision_id
