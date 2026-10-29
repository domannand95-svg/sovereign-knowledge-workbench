from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path, PurePath

from .extract import extract_text


WORDS = re.compile(r"[a-z0-9]+")
SENTENCES = re.compile(r"(?<=[.!?])\s+|\n+")
NEGATION = re.compile(r"\b(?:not|never|no|cannot|can't|doesn't|isn't)\b", re.I)
NOISE = {"a", "an", "and", "addendum", "annex", "appendices", "appendix", "copy",
         "draft", "final", "of", "revised",
         "the", "to", "updated", "v1", "v2", "v3", "version"}
CONTENT_NOISE = NOISE | {"architecture", "document", "evidence", "framework", "project",
                         "research", "system", "technical", "work"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical(value: dict[str, object]) -> tuple[str, str]:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _family_key(path: str, topic: str) -> str:
    stem = PurePath(path.replace("\\", "/")).stem.casefold()
    tokens = [word for word in WORDS.findall(stem) if word not in NOISE and not word.isdigit()]
    identity = "-".join(tokens[:6]) or "unresolved"
    return f"{topic.casefold()}:{identity}"


def _identity_tokens(path: str) -> set[str]:
    stem = PurePath(path.replace("\\", "/")).stem.casefold()
    return {word for word in WORDS.findall(stem) if word not in NOISE and not word.isdigit()}


def _content_tokens(text: str) -> set[str]:
    return {word for word in WORDS.findall(text.casefold())
            if len(word) >= 4 and word not in CONTENT_NOISE}


def _overlap(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / min(len(left), len(right))


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _claim_units(text: str) -> list[tuple[str, str, str]]:
    claims: list[tuple[str, str, str]] = []
    section = "Document body"
    for raw in SENTENCES.split(text):
        excerpt = " ".join(raw.split()).strip()
        if excerpt.startswith("#") or (len(excerpt) < 100 and excerpt.endswith(":")):
            section = excerpt.lstrip("# ").rstrip(":")[:160] or section
            continue
        if len(excerpt) < 25 or len(excerpt) > 500:
            continue
        normalized = " ".join(word for word in WORDS.findall(excerpt.casefold())
                              if word not in {"not", "never", "no", "cannot", "can", "t"})
        if len(normalized.split()) < 5:
            continue
        claims.append((section, normalized, excerpt))
    return claims[:500]


def _verified_text(root: str, relative_path: str, expected_sha256: str) -> str:
    base = Path(root).resolve(strict=True)
    path = (base / Path(relative_path.replace("/", "\\"))).resolve(strict=True)
    if not path.is_relative_to(base) or path.is_symlink() or not path.is_file():
        raise ValueError("Family source escapes its admitted archive root")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != expected_sha256:
        raise ValueError("Family source hash changed after intake")
    text, status = extract_text(path)
    if status != "extracted" or text is None:
        return ""
    return text


def build_family_proposals(database: sqlite3.Connection) -> dict[str, int]:
    """Create inert family, claim, coverage and relationship proposals from stored evidence."""
    rows = database.execute(
        "SELECT s.source_id,s.relative_path,s.source_sha256,COALESCE(c.proposed_topic,'Needs review'),"
        "COALESCE(c.model_summary,''),s.root FROM review_sources s LEFT JOIN archive_review_candidates c "
        "ON c.source_id=s.source_id ORDER BY s.relative_path,s.source_id"
    ).fetchall()
    parent = {row[0]: row[0] for row in rows}

    def find(value: str) -> str:
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(left: str, right: str) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    by_path = {row[1]: row for row in rows}
    relationship_edges = database.execute(
        "SELECT r.source_id,r.related_relative_path FROM archive_review_relationships r "
        "WHERE r.relationship_type IN ('EXACT_DUPLICATE','POSSIBLE_PARENT','VERSION_SIBLING',"
        "'POSSIBLE_SUPERSEDES','HASH_COMPANION')"
    ).fetchall()
    for source_id, related_path in relationship_edges:
        related = by_path.get(related_path)
        if source_id in parent and related:
            union(source_id, related[0])

    text_cache: dict[str, str] = {}
    for row in rows:
        text_cache[row[0]] = _verified_text(row[5], row[1], row[2]) or row[4]
    for index, left in enumerate(rows):
        for right in rows[index + 1:]:
            if _family_key(left[1], left[3]) == _family_key(right[1], right[3]):
                union(left[0], right[0]); continue
            left_name, right_name = _identity_tokens(left[1]), _identity_tokens(right[1])
            left_content, right_content = (_content_tokens(text_cache[left[0]]),
                                           _content_tokens(text_cache[right[0]]))
            name_score = _overlap(left_name, right_name)
            content_score = _jaccard(left_content, right_content)
            strong_name = len(left_name & right_name) >= 3 and name_score >= 0.6
            strong_content = (len(left_content & right_content) >= 50
                              and left[3] == right[3] and content_score >= 0.45)
            if strong_name or strong_content:
                union(left[0], right[0])

    grouped: dict[str, list[tuple[str, str, str, str, str, str]]] = {}
    for row in rows:
        grouped.setdefault(find(row[0]), []).append(row)
    counts = {"families": 0, "members": 0, "claims": 0, "coverage": 0, "relationships": 0}
    for _component, members in sorted(grouped.items()):
        keys = sorted(_family_key(row[1], row[3]) for row in members)
        key = keys[0] if len(keys) == 1 else "connected:" + hashlib.sha256(
            "\n".join(sorted(row[2] for row in members)).encode("utf-8")
        ).hexdigest()[:24]
        contract = {"contract_version": "sovereign.workbench.document-family.v1",
                    "family_key": key, "proposed_topic": members[0][3],
                    "members": sorted({row[2] for row in members}), "authority": "NONE"}
        _, digest = _canonical(contract); family_id = f"FAM-{digest[:24]}"
        counts["families"] += database.execute(
            "INSERT OR IGNORE INTO archive_document_families VALUES(?,?,?,?,?,?,?)",
            (family_id, key, members[0][3], "Normalized filename identity and proposed topic",
             digest, "NONE", _now())).rowcount
        for source_id, path, source_hash, _topic, summary, root in members:
            counts["members"] += database.execute(
                "INSERT OR IGNORE INTO archive_document_family_members VALUES(?,?,?,?)",
                (family_id, source_id, source_hash, "Candidate filename/topic cluster")).rowcount
            source_text = text_cache[source_id]
            for section, normalized, excerpt in _claim_units(source_text or summary):
                claim_contract = {"family_id": family_id, "normalized_claim": normalized}
                _, claim_hash = _canonical(claim_contract); claim_id = f"CLM-{claim_hash[:24]}"
                counts["claims"] += database.execute(
                    "INSERT OR IGNORE INTO archive_family_claims VALUES(?,?,?,?,?)",
                    (claim_id, family_id, normalized, claim_hash, "NONE")).rowcount
                stance = "CONTRADICTS" if NEGATION.search(excerpt) else "ASSERTS"
                counts["coverage"] += database.execute(
                    "INSERT OR IGNORE INTO archive_family_claim_coverage VALUES(?,?,?,?,?)",
                    (claim_id, source_id, stance, section, excerpt)).rowcount
        for index, left in enumerate(members):
            for right in members[index + 1:]:
                if left[2] == right[2]:
                    relation = "EXACT_DUPLICATE_OF"; reason = "Identical SHA-256 digest"
                else:
                    relation = "POSSIBLE_REVISION_OF"; reason = "Same candidate family; content hashes differ"
                proposal = {"family_id": family_id, "source_id": left[0],
                            "related_source_id": right[0], "relationship_type": relation,
                            "reason": reason, "authority": "NONE"}
                _, proposal_hash = _canonical(proposal); proposal_id = f"FRP-{proposal_hash[:24]}"
                counts["relationships"] += database.execute(
                    "INSERT OR IGNORE INTO archive_family_relationship_proposals VALUES(?,?,?,?,?,?,?,?,?)",
                    (proposal_id, family_id, left[0], right[0], relation, reason,
                     proposal_hash, "NONE", _now())).rowcount
    database.commit()
    return counts


def family_report(database: sqlite3.Connection, family_id: str) -> dict[str, object]:
    family = database.execute(
        "SELECT family_key,proposed_topic,rationale,family_sha256 FROM archive_document_families "
        "WHERE family_id=?", (family_id,)).fetchone()
    if not family:
        raise ValueError("Unknown document family")
    members = database.execute(
        "SELECT m.source_id,s.relative_path,m.source_sha256,m.membership_reason FROM "
        "archive_document_family_members m JOIN review_sources s ON s.source_id=m.source_id "
        "WHERE m.family_id=? ORDER BY s.relative_path", (family_id,)).fetchall()
    claims = database.execute(
        "SELECT c.claim_id,c.normalized_claim,v.source_id,v.stance,v.section_label,v.excerpt FROM archive_family_claims c "
        "LEFT JOIN archive_family_claim_coverage v ON v.claim_id=c.claim_id WHERE c.family_id=? "
        "ORDER BY c.claim_id,v.source_id", (family_id,)).fetchall()
    matrix: dict[str, dict[str, object]] = {}
    for claim_id, claim, source_id, stance, section, excerpt in claims:
        item = matrix.setdefault(claim_id, {"claim": claim, "coverage": {}})
        if source_id:
            item["coverage"][source_id] = {"stance": stance, "section": section, "excerpt": excerpt}
    relationships = database.execute(
        "SELECT proposal_id,source_id,related_source_id,relationship_type,reason,proposal_sha256 "
        "FROM archive_family_relationship_proposals WHERE family_id=? ORDER BY proposal_id",
        (family_id,)).fetchall()
    unique_claims = [claim_id for claim_id, claim in matrix.items()
                     if len(claim["coverage"]) == 1]
    contradictory_claims = [claim_id for claim_id, claim in matrix.items()
                            if len({entry["stance"] for entry in claim["coverage"].values()}) > 1]
    return {"contract_version": "sovereign.workbench.family-report.v1", "family_id": family_id,
            "family_key": family[0], "proposed_topic": family[1], "rationale": family[2],
            "family_sha256": family[3],
            "members": [{"source_id": r[0], "relative_path": r[1], "source_sha256": r[2],
                         "reason": r[3]} for r in members], "claim_matrix": matrix,
            "unique_claims": unique_claims, "contradictory_claims": contradictory_claims,
            "relationship_proposals": [{"proposal_id": r[0], "source_id": r[1],
                "related_source_id": r[2], "relationship_type": r[3], "reason": r[4],
                "proposal_sha256": r[5]} for r in relationships], "authority": "NONE"}


def decide_boundary(database: sqlite3.Connection, subject_id: str, subject_sha256: str,
                    decision: str, reviewer: str, reason: str) -> str:
    if decision not in {"APPROVE", "REJECT", "NEEDS_RESEARCH"}:
        raise ValueError("Invalid family-boundary decision")
    if not reviewer.strip() or not reason.strip():
        raise ValueError("Reviewer and reason are required")
    found = database.execute(
        "SELECT family_sha256 FROM archive_document_families WHERE family_id=? UNION ALL "
        "SELECT proposal_sha256 FROM archive_family_relationship_proposals WHERE proposal_id=?",
        (subject_id, subject_id)).fetchone()
    if not found or found[0] != subject_sha256:
        raise ValueError("Family-boundary subject hash mismatch")
    value = {"subject_id": subject_id, "subject_sha256": subject_sha256, "decision": decision,
             "reviewer": reviewer.strip(), "reason": reason.strip()}
    _, digest = _canonical(value); decision_id = f"FBD-{digest[:24]}"
    database.execute("INSERT OR IGNORE INTO archive_family_boundary_decisions VALUES(?,?,?,?,?,?,?,?)",
                     (decision_id, subject_id, subject_sha256, decision, reviewer.strip(),
                      reason.strip(), digest, _now()))
    database.commit(); return decision_id


def generate_research_tickets(database: sqlite3.Connection, family_id: str) -> list[dict[str, object]]:
    report = family_report(database, family_id); member_count = len(report["members"])
    tickets = []
    for claim_id, claim in report["claim_matrix"].items():
        coverage = claim["coverage"]
        stances = {item["stance"] for item in coverage.values()}
        if len(coverage) == member_count and len(stances) == 1:
            continue
        reason = "Contradictory family claims" if len(stances) > 1 else "Claim is not covered by every family member"
        question = f"What reliable primary evidence resolves this claim: {claim['claim']}?"
        value = {"contract_version": "sovereign.workbench.family-research-ticket.v1",
                 "family_id": family_id, "claim_id": claim_id, "question": question,
                 "reason": reason, "authority": "NONE"}
        _, digest = _canonical(value); ticket_id = f"FRT-{digest[:24]}"
        database.execute("INSERT OR IGNORE INTO archive_family_research_tickets VALUES(?,?,?,?,?,?,?,?)",
                         (ticket_id, family_id, claim_id, question, reason, digest, "NONE", _now()))
        tickets.append({**value, "ticket_id": ticket_id, "ticket_sha256": digest})
    database.commit(); return tickets


def import_evidence_return(database: sqlite3.Connection, value: dict[str, object]) -> str:
    required = {"contract_version", "ticket_id", "ticket_sha256", "findings", "citations",
                "uncertainties", "authority", "return_sha256"}
    if set(value) != required or value["authority"] != "NONE":
        raise ValueError("Invalid non-authoritative family evidence return")
    ticket = database.execute("SELECT ticket_sha256 FROM archive_family_research_tickets WHERE ticket_id=?",
                              (value["ticket_id"],)).fetchone()
    if not ticket or ticket[0] != value["ticket_sha256"]:
        raise ValueError("Family research ticket hash mismatch")
    body = {key: value[key] for key in value if key != "return_sha256"}
    _, digest = _canonical(body)
    if digest != value["return_sha256"]:
        raise ValueError("Family evidence return hash mismatch")
    return_id = f"FER-{digest[:24]}"
    database.execute("INSERT OR IGNORE INTO archive_family_evidence_returns VALUES(?,?,?,?,?,?,?,?,?)",
                     (return_id, value["ticket_id"], value["ticket_sha256"],
                      json.dumps(value["findings"], sort_keys=True),
                      json.dumps(value["citations"], sort_keys=True),
                      json.dumps(value["uncertainties"], sort_keys=True), digest, "NONE", _now()))
    database.commit(); return return_id


def propose_synthesis(database: sqlite3.Connection, family_id: str) -> dict[str, object]:
    report = family_report(database, family_id)
    approved = database.execute(
        "SELECT subject_id FROM archive_family_boundary_decisions WHERE subject_id=? AND decision='APPROVE' "
        "ORDER BY decided_at DESC LIMIT 1", (family_id,)).fetchone()
    if not approved:
        raise ValueError("Human family-boundary approval is required before synthesis proposal")
    canonical = max(report["members"], key=lambda item: (sum(
        item["source_id"] in claim["coverage"] for claim in report["claim_matrix"].values()),
        item["relative_path"]))
    unique = [claim_id for claim_id, claim in report["claim_matrix"].items()
              if len(claim["coverage"]) == 1]
    synthesis = {"canonical_candidate": canonical["source_id"], "unique_claims": unique,
                 "unresolved_relationships": [r["proposal_id"] for r in report["relationship_proposals"]],
                 "originals_preserved": True, "execution_authorized": False}
    value = {"contract_version": "sovereign.workbench.family-synthesis.v1",
             "family_id": family_id, "proposed_topic": report["proposed_topic"],
             "proposed_maturity": "Needs review", "proposed_authority": "No authority",
             "canonical_source_id": canonical["source_id"], "synthesis": synthesis,
             "authority": "NONE"}
    encoded, digest = _canonical(value); synthesis_id = f"SYN-{digest[:24]}"
    database.execute("INSERT OR IGNORE INTO archive_family_synthesis_proposals VALUES(?,?,?,?,?,?,?,?,?,?)",
                     (synthesis_id, family_id, value["proposed_topic"], value["proposed_maturity"],
                      value["proposed_authority"], canonical["source_id"], encoded, digest, "NONE", _now()))
    database.commit(); return {**value, "synthesis_id": synthesis_id, "synthesis_sha256": digest}
