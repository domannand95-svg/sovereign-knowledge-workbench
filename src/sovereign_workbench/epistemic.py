from __future__ import annotations

from .model import ArtifactAssessment, FileRecord


RULES = (
    ("HANDOVER", "handover", ("handover", "work mode", "current state", "next work")),
    ("PROPOSED-SPEC", "proposed", ("proposed specification", "proposed spec", "draft specification", "phase v")),
    ("NORMATIVE-SPEC", "claimed_normative", ("normative specification", "status: normative")),
    ("EVIDENCE", "evidence", ("evidence intake", "experimental result", "measurement record", "dataset")),
    ("RESEARCH", "research", ("research", "hypothesis", "conceptual", "literature review", "open question")),
    ("ARCHIVE", "historical", ("superseded", "historical archive", "archived version")),
)


def assess(record: FileRecord, *, exact_duplicate: bool = False) -> ArtifactAssessment:
    searchable = f"{record.relative_path}\n{record.extracted_text or ''}".casefold()
    matches = [(category, maturity) for category, maturity, cues in RULES if any(cue in searchable for cue in cues)]
    if len(matches) == 1:
        category, maturity = matches[0]
        reason = "One deterministic maturity rule matched; human confirmation remains required"
    elif len(matches) > 1:
        category, maturity = "UNKNOWN / NEEDS REVIEW", "ambiguous"
        reason = "Multiple maturity rules matched"
    else:
        category, maturity = "UNKNOWN / NEEDS REVIEW", "unknown"
        reason = "No maturity rule matched"
    return ArtifactAssessment(
        category=category,
        maturity=maturity,
        canonical_status="DUPLICATE" if exact_duplicate else "UNRESOLVED",
        authority_status="non_authoritative_candidate",
        recommended_destination=None,
        move_required="not_determined",
        semantic_drift_risk="high" if category in {"PROPOSED-SPEC", "NORMATIVE-SPEC"} else "unknown",
        reason=reason,
        source="deterministic_epistemic_heuristic",
    )
