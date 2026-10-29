from __future__ import annotations

import hashlib
import html
import json
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

from .family_audit import audit_family_lineage
from .review_batches import BEHAVIOR_CATEGORIES


CATEGORY_TERMS = {
    "AUTHORITY_ESCALATION_REJECTION": {"bypass", "escalation", "forged", "invalid", "unauthorized"},
    "AUTHORITY_BOUNDARY": {"authority", "authorization", "capability", "governance", "permission", "policy"},
    "SCHEMA_CONTRACT_INTERPRETATION": {"contract", "dto", "parser", "schema", "validation"},
    "STRUCTURED_OUTPUT": {"json", "manifest", "output", "structured", "yaml"},
    "ORDINARY_CODING": {"api", "code", "function", "python", "rust", "test"},
    "HALLUCINATION_HANDLING": {"fabricated", "hallucination", "speculative", "unproven",
                               "unsupported", "uncertain", "unknown", "unverified"},
    "REGRESSION": {"baseline", "benchmark", "regression", "test", "validation"},
    "SOVEREIGN_TERMINOLOGY": {"attestation", "canonical", "execution", "receipt", "sovereign"},
    "GENERAL_REASONING": {"causal", "evidence", "inference", "reasoning", "therefore"},
    "OUT_OF_DOMAIN": {"amazon", "art", "community", "culture", "discord", "environmental"},
}
WORDS = re.compile(r"[a-z0-9]+")
SELECTED_SAFETY_PATTERNS = {
    "FRINGE_OR_PERSONAL": re.compile(
        r"(?:10th|tenth)\s+rail|432|4[.]32|15(?:[.]|\s+)3.{0,20}(?:mhz|megahertz)|"
        r"288.{0,100}harmonic|\bfractal\b|\bretrocausality\b|\bvortex\b|\brodin\b|"
        r"arbor\s+inversa|ordo\s+rara\s+avis", re.I | re.S),
    "CNS_STABILIZATION": re.compile(
        r"\bcns\b.{0,160}stabili[sz]|central\s+nervous\s+system.{0,160}stabili[sz]", re.I | re.S),
    "EMAIL_ADDRESS": re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I),
    "AUSTRALIAN_PHONE": re.compile(r"(?:\+?61\s?[2-478]|0[2-478])(?:[\s-]?\d){8}\b"),
}
STRONG_CATEGORY_PATTERNS = {
    "HALLUCINATION_HANDLING": re.compile(
        r"(?:\bno\s+fabrication(?:/falsification)?\b|\bfabricat(?:ed|ion)\b|"
        r"\bfalsification\b|\bwithout\s+inventing\b|\bconfabulat\w*\b|"
        r"\bfalse\s+claims?\b|\bunsubstantiated\b|\bhallucination\b)", re.I),
    "SCHEMA_CONTRACT_INTERPRETATION": re.compile(
        r"(?:\b(?:pub\s+)?(?:struct|enum)\s+[A-Za-z_][A-Za-z0-9_]*\b|"
        r"\bserde(?:::|_)|\bserialize\b|\bdeserialize\b|\bdata\s+model\b|"
        r"\brequired\s+fields?\b|\bcontract[_ -]?version\b|\bschema\b|\bdto\b|"
        r"\bparser\s+(?:boundary|validation|contract)\b)", re.I),
}


def _category(heading: str, excerpt: str) -> tuple[str, str, int]:
    text = f"{heading}\n{excerpt}"
    for category in ("HALLUCINATION_HANDLING", "SCHEMA_CONTRACT_INTERPRETATION"):
        matches = STRONG_CATEGORY_PATTERNS[category].findall(text)
        if matches:
            return category, f"Matched {len(matches)} strong controlled behavior patterns", len(matches)
    tokens = set(WORDS.findall(text.casefold()))
    scored = sorted(((len(tokens & terms), category) for category, terms in CATEGORY_TERMS.items()),
                    key=lambda item: (-item[0], item[1]))
    score, category = scored[0]
    return category, f"Matched {score} controlled behavior terms", score


def build_section_dataset_candidates(database: sqlite3.Connection) -> dict[str, object]:
    """Derive non-authoritative section examples from reviewed maps without allocating them."""
    family_audit = audit_family_lineage(database)
    family_by_source = {source_id: family["family_id"] for family in family_audit["families"]
                        for source_id in family["source_ids"]}
    latest_maps = {row[0]: row[1] for row in database.execute(
        "WITH ranked AS (SELECT source_id,decision,ROW_NUMBER() OVER(PARTITION BY source_id "
        "ORDER BY decided_at DESC,decision_id DESC) rank FROM archive_document_map_decisions) "
        "SELECT source_id,decision FROM ranked WHERE rank=1"
    )}
    excluded = {row[0] for row in database.execute(
        "SELECT DISTINCT section_id FROM archive_section_exclusions"
    )}
    contaminated_sources = {row[0] for row in database.execute(
        "SELECT DISTINCT x.source_id FROM archive_document_sections x "
        "JOIN archive_section_exclusions e ON e.section_id=x.section_id"
    )}
    directed = {row[0] for row in database.execute(
        "SELECT DISTINCT section_id FROM archive_section_directives"
    )}
    directed_sources = {row[0] for row in database.execute(
        "SELECT DISTINCT x.source_id FROM archive_document_sections x "
        "JOIN archive_section_directives d ON d.section_id=x.section_id"
    )}
    routed = {row[0] for row in database.execute(
        "SELECT DISTINCT source_id FROM archive_repository_artifact_routes"
    )}
    proposals: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for section_id, topic, confidence in database.execute(
        "SELECT section_id,proposed_topic,confidence FROM archive_section_family_proposals "
        "ORDER BY section_id,confidence DESC,proposed_topic"
    ):
        proposals[section_id].append((topic, confidence))
    rows = database.execute(
        "SELECT x.section_id,x.section_sha256,x.source_id,x.source_sha256,x.ordinal,x.heading,"
        "x.bounded_excerpt,s.relative_path FROM archive_document_sections x "
        "JOIN review_sources s ON s.source_id=x.source_id ORDER BY s.relative_path,x.ordinal"
    ).fetchall()
    candidates = []
    omitted = Counter()
    for section_id, section_hash, source_id, source_hash, ordinal, heading, excerpt, path in rows:
        decision = latest_maps.get(source_id)
        if decision is None:
            omitted["UNREVIEWED_MAP"] += 1; continue
        if source_id in routed:
            omitted["REPOSITORY_ARTIFACT"] += 1; continue
        if source_id in contaminated_sources:
            omitted["CONTAMINATED_SOURCE_QUARANTINE"] += 1; continue
        if source_id in directed_sources:
            omitted["RESEARCH_DIRECTIVE_SOURCE_QUARANTINE"] += 1; continue
        if section_id in excluded:
            omitted["CONTAMINATION_EXCLUDED"] += 1; continue
        if section_id in directed:
            omitted["RESEARCH_DIRECTIVE"] += 1; continue
        section_topics = proposals.get(section_id, [])
        if not section_topics:
            omitted["NO_TOPIC_PROPOSAL"] += 1; continue
        category, category_reason, category_score = _category(heading, excerpt)
        value = {"contract_version": "sovereign.workbench.section-training-candidate.v1",
                 "source_id": source_id, "source_sha256": source_hash,
                 "source_family_id": family_by_source[source_id], "relative_path": path,
                 "section_id": section_id, "section_sha256": section_hash, "ordinal": ordinal,
                 "input": {"heading": heading, "bounded_excerpt": excerpt},
                 "expected": {"document_map_decision": decision,
                              "proposed_topics": [topic for topic, _confidence in section_topics]},
                 "behavior_category": category, "category_reason": category_reason,
                 "category_score": category_score, "allocation": "UNALLOCATED",
                 "requires_human_allocation": True, "authority": "NONE"}
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        value["candidate_sha256"] = hashlib.sha256(encoded.encode()).hexdigest()
        value["candidate_id"] = f"SDC-{value['candidate_sha256'][:24]}"
        candidates.append(value)
    counts = Counter(candidate["behavior_category"] for candidate in candidates)
    return {"contract_version": "sovereign.workbench.section-dataset-candidate-audit.v1",
            "authority": "NONE", "training_authorized": False,
            "candidate_count": len(candidates), "candidates": candidates,
            "category_counts": {category: counts[category] for category in sorted(BEHAVIOR_CATEGORIES)},
            "omitted_counts": dict(sorted(omitted.items())),
            "family_count": len({candidate["source_family_id"] for candidate in candidates})}


def propose_family_safe_split(candidates: list[dict[str, object]]) -> dict[str, object]:
    """Propose, but never approve, a 300/100 split with whole-family isolation."""
    by_family: dict[str, list[dict[str, object]]] = defaultdict(list)
    for candidate in candidates:
        by_family[str(candidate["source_family_id"])].append(candidate)
    families = sorted(by_family, key=lambda family_id: hashlib.sha256(family_id.encode()).hexdigest())
    evaluation_families: list[str] = []
    evaluation_pool = 0
    for family_id in families:
        if evaluation_pool >= 100:
            break
        evaluation_families.append(family_id)
        evaluation_pool += len(by_family[family_id])
    train_families = [family_id for family_id in families if family_id not in evaluation_families]

    def balanced_pick(family_ids: list[str], limit: int) -> list[dict[str, object]]:
        pool = [item for family_id in family_ids for item in by_family[family_id]]
        buckets: dict[str, list[dict[str, object]]] = defaultdict(list)
        for item in pool:
            buckets[str(item["behavior_category"])].append(item)
        selected = []
        while len(selected) < limit:
            progressed = False
            for category in sorted(BEHAVIOR_CATEGORIES):
                if buckets[category] and len(selected) < limit:
                    selected.append(buckets[category].pop(0)); progressed = True
            if not progressed:
                break
        return selected

    evaluation = balanced_pick(evaluation_families, 100)
    training = balanced_pick(train_families, 300)
    combined = Counter(str(item["behavior_category"]) for item in training + evaluation)
    blockers = []
    if len(training) < 300:
        blockers.append(f"Training proposal shortfall: {300-len(training)}")
    if len(evaluation) < 100:
        blockers.append(f"Held-out proposal shortfall: {100-len(evaluation)}")
    missing = sorted(category for category in BEHAVIOR_CATEGORIES if combined[category] < 25)
    if missing:
        blockers.append("Categories below 25 candidates: " + ", ".join(missing))
    selected = training + evaluation
    safety_checks = {}
    for name, pattern in SELECTED_SAFETY_PATTERNS.items():
        count = sum(bool(pattern.search(
            f"{item['relative_path']}\n{item['input']['heading']}\n{item['input']['bounded_excerpt']}"
        )) for item in selected)
        safety_checks[name] = count
        if count:
            blockers.append(f"Selected safety check {name} matched {count} examples")
    return {"contract_version": "sovereign.workbench.section-split-proposal.v1",
            "training_candidate_ids": [item["candidate_id"] for item in training],
            "held_out_candidate_ids": [item["candidate_id"] for item in evaluation],
            "training_family_ids": train_families,
            "held_out_family_ids": evaluation_families,
            "family_leakage": sorted(set(train_families) & set(evaluation_families)),
            "category_counts": {category: combined[category] for category in sorted(BEHAVIOR_CATEGORIES)},
            "selected_safety_checks": safety_checks,
            "ready_for_human_allocation_review": not blockers,
            "blockers": blockers, "approved": False, "training_authorized": False,
            "authority": "NONE"}


def export_section_dataset_audit(database: sqlite3.Connection, output: Path) -> dict[str, object]:
    if output.exists():
        raise ValueError("Section dataset audit output already exists")
    audit = build_section_dataset_candidates(database)
    audit["split_proposal"] = propose_family_safe_split(audit["candidates"])
    encoded = json.dumps(audit, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    audit["audit_sha256"] = hashlib.sha256(encoded.encode()).hexdigest()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(audit, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                      encoding="utf-8", newline="\n")
    return audit


def export_section_dataset_summary(audit_path: Path, output: Path) -> None:
    """Create a compact human review page; it grants no dataset or training authority."""
    if output.exists():
        raise ValueError("Dataset review summary already exists")
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    split = audit["split_proposal"]
    selected_ids = set(split["training_candidate_ids"] + split["held_out_candidate_ids"])
    selected = [item for item in audit["candidates"] if item["candidate_id"] in selected_ids]
    examples: dict[str, list[dict[str, object]]] = defaultdict(list)
    for item in selected:
        category = str(item["behavior_category"])
        if len(examples[category]) < 2:
            examples[category].append(item)
    rows = "".join(
        f"<tr><td>{html.escape(category)}</td><td>{count}</td></tr>"
        for category, count in split["category_counts"].items())
    cards = []
    for category in sorted(examples):
        snippets = "".join(
            "<li><strong>" + html.escape(str(item["input"]["heading"])) + "</strong><br>" +
            html.escape(str(item["input"]["bounded_excerpt"])[:360]) + "</li>"
            for item in examples[category])
        cards.append(f"<section><h3>{html.escape(category)}</h3><ul>{snippets}</ul></section>")
    checks = "".join(
        f"<li>{html.escape(name)}: <strong>{count}</strong></li>"
        for name, count in split["selected_safety_checks"].items())
    status = "READY FOR YOUR REVIEW" if split["ready_for_human_allocation_review"] else "BLOCKED"
    page = f"""<!doctype html><html><head><meta charset='utf-8'><title>Dataset review summary</title>
<style>body{{font:18px system-ui;max-width:1050px;margin:40px auto;padding:0 24px;color:#18202a}}h1{{font-size:34px}}.status{{padding:18px;border-radius:12px;background:#e8f5e9}}table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #ccd5df;padding:9px}}section{{border-top:1px solid #ccd5df;padding:12px 0}}li{{margin:10px 0}}code{{word-break:break-all}}</style></head><body>
<h1>Workbench dataset review</h1><div class='status'><strong>{status}</strong><br>
300 proposed training examples · 100 proposed held-out examples · training is not authorized.</div>
<h2>Safety checks</h2><ul>{checks}</ul><p>Family leakage: <strong>{len(split['family_leakage'])}</strong></p>
<h2>Balance</h2><table><tr><th>Behavior</th><th>Selected examples</th></tr>{rows}</table>
<h2>Representative examples</h2><p>Two examples per category are shown below. You do not need to read all 400.</p>{''.join(cards)}
<h2>Decision</h2><p>If this summary is acceptable, return to Codex and say <strong>“I approve Audit-007 for manifest materialization.”</strong> Otherwise describe what should be removed or researched.</p>
<p>Audit hash: <code>{html.escape(str(audit['audit_sha256']))}</code></p></body></html>"""
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(page, encoding="utf-8", newline="\n")


def materialize_approved_section_split(audit_path: Path, output_dir: Path, *,
                                       expected_audit_sha256: str, reviewer: str) -> dict[str, object]:
    """Materialize an explicitly approved proposal; never launch or authorize training."""
    if output_dir.exists():
        raise ValueError("Approved dataset destination already exists")
    if not reviewer.strip():
        raise ValueError("A human reviewer is required")
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    stored_hash = audit.get("audit_sha256")
    hash_input = {key: audit[key] for key in audit if key != "audit_sha256"}
    encoded = json.dumps(hash_input, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    calculated_hash = hashlib.sha256(encoded.encode()).hexdigest()
    if stored_hash != calculated_hash or stored_hash != expected_audit_sha256:
        raise ValueError("Approved audit hash mismatch")
    split = audit["split_proposal"]
    if (not split["ready_for_human_allocation_review"] or split["blockers"]
            or split["family_leakage"] or any(split["selected_safety_checks"].values())):
        raise ValueError("Dataset proposal fails the approval gate")
    training_ids = split["training_candidate_ids"]
    held_out_ids = split["held_out_candidate_ids"]
    if len(training_ids) != 300 or len(held_out_ids) != 100:
        raise ValueError("Approved split must contain exactly 300 train and 100 held-out examples")
    if set(training_ids) & set(held_out_ids):
        raise ValueError("A candidate crosses the train and held-out boundary")
    by_id = {item["candidate_id"]: item for item in audit["candidates"]}
    if len(by_id) != len(audit["candidates"]):
        raise ValueError("Dataset audit contains duplicate candidate identifiers")

    def examples(ids: list[str]) -> list[dict[str, object]]:
        values = []
        for candidate_id in ids:
            item = by_id.get(candidate_id)
            if item is None:
                raise ValueError("Approved split references an unknown candidate")
            values.append({"contract_version": "sovereign.workbench.section-training-example.v1",
                           "example_id": candidate_id, "source_id": item["source_id"],
                           "source_family_id": item["source_family_id"],
                           "section_id": item["section_id"], "behavior_category": item["behavior_category"],
                           "instruction": "Classify this bounded archive section without granting authority.",
                           "input": item["input"], "expected": item["expected"],
                           "authority": "NONE"})
        return values

    training, held_out = examples(training_ids), examples(held_out_ids)
    output_dir.mkdir(parents=True)
    train_path, held_path = output_dir / "training.jsonl", output_dir / "held-out.jsonl"
    for path, values in ((train_path, training), (held_path, held_out)):
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            for value in values:
                stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")
    approval = {"contract_version": "sovereign.workbench.section-dataset-approval.v1",
                "audit_sha256": stored_hash, "reviewer": reviewer.strip(),
                "decision": "APPROVE_FOR_MANIFEST_MATERIALIZATION",
                "training_authorized": False, "authority": "NONE"}
    approval_encoded = json.dumps(approval, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    approval["approval_sha256"] = hashlib.sha256(approval_encoded.encode()).hexdigest()
    approval_path = output_dir / "approval.json"
    approval_path.write_text(json.dumps(approval, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    manifest = {"contract_version": "sovereign.workbench.section-dataset-manifest.v1",
                "audit_sha256": stored_hash, "approval_sha256": approval["approval_sha256"],
                "training": {"path": train_path.name, "examples": 300,
                             "sha256": hashlib.sha256(train_path.read_bytes()).hexdigest()},
                "held_out": {"path": held_path.name, "examples": 100,
                             "sha256": hashlib.sha256(held_path.read_bytes()).hexdigest()},
                "family_leakage": [], "training_authorized": False, "authority": "NONE"}
    manifest_encoded = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    manifest["manifest_sha256"] = hashlib.sha256(manifest_encoded.encode()).hexdigest()
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest
