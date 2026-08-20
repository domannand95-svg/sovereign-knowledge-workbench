from __future__ import annotations

import re

from .model import Classification, FileRecord, Finding


MODULE_RULES = {
    "governance": ("policy", "governance", "regulation", "legislation", "minister"),
    "research": ("research", "study", "methodology", "hypothesis", "literature"),
    "evidence": ("evidence", "finding", "dataset", "measurement", "result"),
    "correspondence": ("dear ", "regards", "email", "recipient", "subject:"),
    "finance": ("invoice", "payment", "financial statement", "purchase receipt"),
    "legal": ("legal", "contract", "agreement", "liability", "confidential"),
}

SENSITIVE_PATTERNS = {
    "email_address": re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I),
    "phone_number": re.compile(r"(?<!\d)(?:\+?61|0)[2-478](?:[ -]?\d){8}(?!\d)"),
    "payment_card_candidate": re.compile(r"\b(?:\d[ -]*?){13,19}\b"),
    "australian_tax_file_number_candidate": re.compile(r"\b\d{3}[ -]?\d{3}[ -]?\d{3}\b"),
}

RESEARCH_CUES = (
    "further research", "citation needed", "verify source", "verify claim",
    "to be confirmed", "research required", "open question",
)


def classify(record: FileRecord) -> Classification:
    text = (record.extracted_text or "").casefold()
    scores = {module: sum(text.count(term) for term in terms) for module, terms in MODULE_RULES.items()}
    module, score = max(scores.items(), key=lambda item: (item[1], item[0]))
    if score == 0:
        module = "unclassified"
    confidence = min(0.95, 0.35 + score * 0.1) if score else 0.0
    labels = tuple(sorted(name for name, value in scores.items() if value > 0))
    summary = " ".join((record.extracted_text or "").split())[:280]
    return Classification(module, confidence, labels, summary, "deterministic_heuristic")


def privacy_findings(record: FileRecord) -> list[Finding]:
    text = record.extracted_text or ""
    findings: list[Finding] = []
    for kind, pattern in SENSITIVE_PATTERNS.items():
        count = len(pattern.findall(text))
        if count:
            findings.append(Finding(
                kind=kind,
                severity="high" if "card" in kind or "tax" in kind else "medium",
                message=f"Detected {count} possible {kind.replace('_', ' ')} value(s); review before disclosure",
                source_path=record.relative_path,
            ))
    return findings


def research_gaps(record: FileRecord) -> list[dict[str, str]]:
    if not record.relative_path.casefold().endswith((".md", ".txt", ".rst", ".html", ".htm")):
        return []
    text = record.extracted_text or ""
    queue = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if any(cue in line.casefold() for cue in RESEARCH_CUES):
            queue.append({
                "source_path": record.relative_path,
                "location": f"line:{line_number}",
                "question": line.strip()[:500],
                "status": "needs_review",
            })
    return queue
