from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone


DIRECTIVES = {
    "NEURAL_BUS_15_3MHZ": (
        re.compile(r"(?:15[ .]?3\s*mhz|neural\s+bus)", re.I), "SPECULATIVE_HOLD",
        "15.3 MHz neural-bus claims are speculative and are not a current design input"),
    "INDRAS_NET_6G_MESH": (
        re.compile(r"(?:indra[’']s\s+net|6g\s+(?:isac\s+)?mesh|6g\s+mesh\s+network)", re.I),
        "CLOSED_LAB_RESEARCH",
        "Indra's Net and 6G mesh are one bounded closed-research-lab networking family"),
    "COGNITIVE_NEGENTROPY": (
        re.compile(r"(?:cognitive\s+negentropy|information.theoretic\s+thermodynamics)", re.I),
        "ASSESS_CONTAMINATION",
        "Requires assessment for Arbor Inversa or non-materialist conceptual contamination"),
    "MOLECULAR_MINT_TRANSMUTATION": (
        re.compile(r"(?:molecular\s+mint|elemental\s+transmutation)", re.I),
        "SUPERSEDED_NONVIABLE",
        "Archived proof-of-concept later reclassified as non-viable"),
    "ACCELERATOR_ISOTOPE_PRODUCTION": (
        re.compile(r"(?:particle\s+accelerator.{0,160}isotope|"
                   r"isotope\s+production.{0,160}particle\s+accelerator)", re.I | re.S),
        "RESEARCH_REQUIRED",
        "Highly speculative; independent feasibility, safety, regulatory, and economic research required"),
    "TCF_FUTURE_COMPUTE": (
        re.compile(r"(?:tcf.?01a|asynchronous\s+neuromorphic|ternary\s+triadic|"
                   r"multi.?valued\s+comput|non.?linear\s+logic)", re.I),
        "FUTURE_FUNDING_RESEARCH",
        "Sovereign OS future hardware/software research only if evidence and funding support it"),
    "MPCVD_RETAINED_RESEARCH": (
        re.compile(r"(?:\bmpcvd\b|microwave\s+plasma\s+chemical\s+vapor\s+deposition)", re.I),
        "RESEARCH_REQUIRED",
        "Retained materials-processing avenue; validate feedstock, process window, product claims, "
        "energy demand, safety, and economics independently of Molecular Mint claims"),
    "CNS_STABILIZATION_RESEARCH": (
        re.compile(r"(?:\bcns\b.{0,160}stabili[sz]|central\s+nervous\s+system.{0,160}stabili[sz]|"
                   r"stabili[sz].{0,160}(?:\bcns\b|central\s+nervous\s+system)|"
                   r"atmospheric.{0,160}(?:neuro.?active|central\s+nervous\s+system))", re.I | re.S),
        "RESEARCH_REQUIRED",
        "CNS-stabilization and atmospheric neuro-active claims require independent medical, "
        "toxicological, ethical, regulatory, and efficacy review"),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _hash(value: dict[str, object]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def apply_concept_directives(database: sqlite3.Connection, *, reviewer: str) -> dict[str, int]:
    if not reviewer.strip():
        raise ValueError("A human reviewer is required for concept directives")
    rows = database.execute(
        "SELECT section_id,source_sha256,heading,bounded_excerpt "
        "FROM archive_document_sections ORDER BY section_id"
    ).fetchall()
    counts = {code: 0 for code in DIRECTIVES}
    for section_id, source_sha256, heading, excerpt in rows:
        content = f"{heading}\n{excerpt}"
        for code, (pattern, disposition, reason) in DIRECTIVES.items():
            if not pattern.search(content):
                continue
            value = {"contract_version": "sovereign.workbench.section-directive.v1",
                     "section_id": section_id, "source_sha256": source_sha256,
                     "concept_code": code, "disposition": disposition, "reason": reason,
                     "reviewer": reviewer.strip(), "authority": "NONE"}
            digest = _hash(value)
            counts[code] += database.execute(
                "INSERT OR IGNORE INTO archive_section_directives VALUES(?,?,?,?,?,?,?,?,?,?)",
                (f"DIR-{digest[:24]}", section_id, source_sha256, code, disposition, reason,
                 reviewer.strip(), digest, "NONE", _now())).rowcount
    database.commit()
    return {"inserted": sum(counts.values()), **counts}
