from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


EXCLUSION_SCOPE = "RESEARCH_SYNTHESIS_PUBLICATION_TRAINING"
CONTAMINATION_MARKERS = {
    "ORA_PERSONAL": re.compile(r"(?<![a-z0-9])(?:o\s*\.\s*r\s*\.\s*a\s*\.?|ordo\s+rara\s+avis)(?![a-z0-9])", re.I),
    "FREQUENCY_432": re.compile(
        r"(?<!\d)(?:432(?:\\text\s*[{])?\s*(?:hz|hertz)[}]?|"
        r"4[.]32(?:\\text\s*[{])?\s*(?:mhz|megahertz)[}]?)(?![a-z])", re.I),
    "FRACTAL_RESONANCE": re.compile(r"\b(?:unified\s+fractal\s+resonance|fractal\s+(?:harmonic\s+)?resonance)\b", re.I),
    "VORTEX_MATHEMATICS": re.compile(r"\b(?:vortex[- ]based\s+mathematics|vortex\s+mathematics|modular[- ]9\s+(?:arithmetic|numerology))\b", re.I),
    "SOLAR_VORTEX": re.compile(r"\b(?:solar[- ]vortex|solar\s+harmonic\s+restoration)\b", re.I),
    "ARBOR_INVERSA_SPECULATIVE": re.compile(r"\barbor\s+inversa\b", re.I),
    "UNIFIED_FRACTAL_RESONANCE": re.compile(
        r"(?:\bunified\s+fractal\s+resonance(?:\s+framework)?\b|\bufrf\b)", re.I),
    "GOLDEN_RATIO_ENGINEERING": re.compile(
        r"(?:\bgolden\s+ratio\b|\bphi\s*(?:≈|~=|=)\s*1[.]618\b)", re.I),
    "FIBONACCI_SEXAGESIMAL_TIMING": re.compile(
        r"(?:\bfibonacci.sexagesimal\b|\bbase.?60\s+timing\b|\bsexagesimal\s+timing\b)", re.I),
    "NEURAL_BUS_15_3MHZ": re.compile(r"(?:15[ .]?3\s*mhz|neural\s+bus)", re.I),
    "COGNITIVE_NEGENTROPY_REVIEW": re.compile(
        r"(?:cognitive\s+negentropy|information.theoretic\s+thermodynamics)", re.I),
    "MOLECULAR_MINT_NONVIABLE": re.compile(
        r"(?:molecular\s+mint|elemental\s+transmutation)", re.I),
    "RODIN_VORTEX_CONTAMINATION": re.compile(
        r"(?:\brodin(?:\s+(?:weights?|vortex))?\b|\bvortex(?:es)?\b)", re.I),
    "FRACTAL_COSMOLOGY_CONTAMINATION": re.compile(
        r"(?:\bfractal\b|\bretrocausality\b|\bomega\s+point\b|\barchons?\b)", re.I),
    "HARMONIC_NUMEROLOGY_CONTAMINATION": re.compile(
        r"(?:\bharmonic\s+(?:baseline|bases?|molecular\s+assembler)\b|"
        r"\bvigesimal.{0,80}\bharmonics?\b|\bfrequency\s+of\s+the\s+earth\b|"
        r"\bcymatics?\b|\bpisano\s+periodicity\b|"
        r"\bfibonacci.{0,120}(?:timing|cycle|base.?60|sexagesimal)\b)", re.I | re.S),
    "RITUAL_COSMIC_TECH_CONTAMINATION": re.compile(
        r"(?:\britual\s+(?:dashboard|evidence|insight)\b|\bzodiac\s+wheel\b|"
        r"\bvajra\s+bridge\b|\bbindu\s+pulse\b|\bsigillum\b|"
        r"\bcosmic\s+sensor\b|\bplanetary\s+resonance\b)", re.I),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _hash(value: dict[str, object]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def apply_contamination_exclusions(database: sqlite3.Connection, *, reviewer: str) -> dict[str, int]:
    if not reviewer.strip():
        raise ValueError("A human reviewer is required for contamination exclusions")
    rows = database.execute(
        "SELECT x.section_id,x.source_sha256,x.heading,x.bounded_excerpt FROM archive_document_sections x "
        "ORDER BY x.section_id"
    ).fetchall()
    counts = {code: 0 for code in CONTAMINATION_MARKERS}
    for section_id, source_sha256, heading, excerpt in rows:
        content = f"{heading}\n{excerpt}"
        for code, pattern in CONTAMINATION_MARKERS.items():
            if not pattern.search(content):
                continue
            value = {"contract_version": "sovereign.workbench.section-exclusion.v1",
                     "section_id": section_id, "source_sha256": source_sha256,
                     "marker_code": code,
                     "reason": "Explicit user-directed corpus contamination marker matched",
                     "exclusion_scope": EXCLUSION_SCOPE, "reviewer": reviewer.strip(),
                     "authority": "NONE"}
            digest = _hash(value); exclusion_id = f"EXC-{digest[:24]}"
            counts[code] += database.execute(
                "INSERT OR IGNORE INTO archive_section_exclusions VALUES(?,?,?,?,?,?,?,?,?,?)",
                (exclusion_id, section_id, source_sha256, code, value["reason"], EXCLUSION_SCOPE,
                 reviewer.strip(), digest, "NONE", _now())).rowcount
    database.commit()
    return {"inserted": sum(counts.values()), **counts}


def generate_topic_research_tickets(database: sqlite3.Connection, *, max_sections: int = 20) -> dict[str, int]:
    if max_sections < 1 or max_sections > 50:
        raise ValueError("Research tickets must contain between 1 and 50 sections")
    accepted_sources = database.execute(
        "WITH latest AS (SELECT m.*,ROW_NUMBER() OVER (PARTITION BY source_id "
        "ORDER BY decided_at DESC,decision_id DESC) rank FROM archive_document_map_decisions m) "
        "SELECT source_id,source_sha256 FROM latest WHERE rank=1 AND decision='MAP_ACCEPTED' "
        "ORDER BY source_id"
    ).fetchall()
    inserted = 0; excluded_sections = 0
    for source_id, source_sha256 in accepted_sources:
        rows = database.execute(
            "SELECT p.proposed_topic,x.section_id,x.section_sha256,x.heading FROM archive_document_sections x "
            "JOIN archive_section_family_proposals p ON p.section_id=x.section_id "
            "LEFT JOIN archive_section_exclusions e ON e.section_id=x.section_id "
            "WHERE x.source_id=? AND e.exclusion_id IS NULL ORDER BY p.proposed_topic,x.ordinal,p.proposal_id",
            (source_id,)).fetchall()
        excluded_sections += database.execute(
            "SELECT COUNT(DISTINCT x.section_id) FROM archive_document_sections x "
            "JOIN archive_section_exclusions e ON e.section_id=x.section_id WHERE x.source_id=?",
            (source_id,)).fetchone()[0]
        topics: dict[str, list[tuple[str, str, str]]] = {}
        for topic, section_id, section_hash, heading in rows:
            topics.setdefault(topic, []).append((section_id, section_hash, heading))
        for topic, sections in sorted(topics.items()):
            for start in range(0, len(sections), max_sections):
                chunk = sections[start:start + max_sections]
                section_ids = [row[0] for row in chunk]; section_hashes = [row[1] for row in chunk]
                headings = [row[2] for row in chunk[:5]]
                question = (f"What reliable primary evidence supports, contradicts, or bounds the {topic} "
                            f"claims in these sections: {'; '.join(headings)}?")
                value = {"contract_version": "sovereign.workbench.section-research-ticket.v1",
                         "source_id": source_id, "source_sha256": source_sha256,
                         "proposed_topic": topic, "section_ids": section_ids,
                         "section_hashes": section_hashes, "question": question,
                         "reason": "Accepted document map; topic-separated non-excluded section group",
                         "status": "DRAFT", "authority": "NONE"}
                digest = _hash(value); ticket_id = f"SRT-{digest[:24]}"
                inserted += database.execute(
                    "INSERT OR IGNORE INTO archive_section_research_tickets VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (ticket_id, source_id, source_sha256, topic, json.dumps(section_ids),
                     json.dumps(section_hashes), question, value["reason"], digest,
                     "DRAFT", "NONE", _now())).rowcount
    database.commit()
    return {"inserted": inserted, "accepted_sources": len(accepted_sources),
            "excluded_sections": excluded_sections}


def export_topic_research_tickets(database: sqlite3.Connection, output: Path) -> int:
    if output.exists():
        raise ValueError("Research ticket export already exists")
    rows = database.execute(
        "SELECT ticket_id,source_id,source_sha256,proposed_topic,section_ids_json,"
        "section_hashes_json,question,reason,ticket_sha256,status,authority "
        "FROM archive_section_research_tickets ORDER BY proposed_topic,ticket_id"
    ).fetchall()
    excluded = {row[0] for row in database.execute(
        "SELECT DISTINCT section_id FROM archive_section_exclusions"
    ).fetchall()}
    rows = [row for row in rows if not (set(json.loads(row[4])) & excluded)]
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            value = {"contract_version": "sovereign.workbench.section-research-ticket.v1",
                     "ticket_id": row[0], "source_id": row[1], "source_sha256": row[2],
                     "proposed_topic": row[3], "section_ids": json.loads(row[4]),
                     "section_hashes": json.loads(row[5]), "question": row[6],
                     "reason": row[7], "ticket_sha256": row[8], "status": row[9],
                     "authority": row[10]}
            stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")
    return len(rows)
