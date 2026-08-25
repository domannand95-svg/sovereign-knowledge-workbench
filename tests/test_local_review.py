from pathlib import Path

import pytest

from sovereign_workbench.intake import scan_files
from sovereign_workbench.local_review import ReviewController, propose_research_question
from sovereign_workbench.review_batches import admit, connect, create_next, store_candidates


def prepared(tmp_path: Path):
    source = tmp_path / "archive"
    source.mkdir()
    manual = source / "uncertain.txt"
    manual.write_text("A speculative system claim requiring primary evidence.", encoding="utf-8")
    recovery = source / "broken.docx"
    recovery.write_text("not a valid DOCX container", encoding="utf-8")
    database = connect(tmp_path / "review.db")
    admit(database, source, scan_files(source, include_suffixes={".txt", ".docx"}))
    batch = create_next(database)
    candidates = []
    for item in batch["items"]:
        candidates.append({
            "source_id": item["source_id"],
            "model_summary": f"Summary for {item['relative_path']}",
            "proposed_topic": "research",
            "proposed_maturity": "unknown",
            "proposed_authority": "non_authoritative_candidate",
            "proposal_reason": "bounded deterministic and model proposal",
            "classification_confidence": 0.4,
            "classification_abstained": True,
            "risk_flags": [],
        })
    store_candidates(database, batch["batch_id"], candidates)
    return source, database


def test_review_is_one_file_at_a_time_and_records_human_without_file_mutation(tmp_path: Path):
    source, database = prepared(tmp_path)
    controller = ReviewController(database, "Dominic Annand")
    items = controller.pending()
    assert [item.source_path for item in items] == ["broken.docx", "uncertain.txt"]
    item = items[0]
    before = (source / item.source_path).read_bytes()
    assert controller.accept(item, "QUARANTINE", note="Invalid container; preserve for recovery") == 1
    assert (source / item.source_path).read_bytes() == before
    row = database.execute(
        "SELECT decision,reviewer,authority,canonical_status FROM archive_review_decisions"
    ).fetchone()
    assert row == ("QUARANTINE", "Dominic Annand", "Non-authoritative candidate", "UNRESOLVED")
    assert [pending.source_path for pending in controller.pending()] == ["uncertain.txt"]
    database.close()


def test_source_hash_change_blocks_acceptance(tmp_path: Path):
    source, database = prepared(tmp_path)
    controller = ReviewController(database, "Dominic")
    item = next(value for value in controller.pending() if value.source_path == "uncertain.txt")
    (source / item.source_path).write_text("changed after admission", encoding="utf-8")
    with pytest.raises(ValueError, match="size changed|hash changed"):
        controller.accept(item, "REJECT", note="Changed content")
    assert database.execute("SELECT COUNT(*) FROM archive_review_decisions").fetchone()[0] == 0
    database.close()


def test_research_requires_and_proposes_a_specific_question(tmp_path: Path):
    _, database = prepared(tmp_path)
    controller = ReviewController(database, "Dominic")
    item = next(value for value in controller.pending() if value.source_path == "uncertain.txt")
    proposal = propose_research_question(item)
    assert "primary evidence" in proposal and "uncertainty" in proposal
    with pytest.raises(ValueError, match="requires a research question"):
        controller.accept(item, "NEEDS_RESEARCH", note="More evidence needed")
    assert controller.accept(item, "NEEDS_RESEARCH", note="More evidence needed",
                             research_question=proposal) == 1
    stored = database.execute(
        "SELECT research_question,reviewer FROM archive_review_decisions"
    ).fetchone()
    assert stored == (proposal, "Dominic")
    database.close()


def test_unknown_decision_and_empty_reviewer_fail_closed(tmp_path: Path):
    _, database = prepared(tmp_path)
    with pytest.raises(ValueError, match="reviewer name"):
        ReviewController(database, " ")
    controller = ReviewController(database, "Dominic")
    with pytest.raises(ValueError, match="invalid controlled value"):
        controller.accept(controller.pending()[0], "DELETE", note="Not permitted")
    assert database.execute("SELECT COUNT(*) FROM archive_review_decisions").fetchone()[0] == 0
    database.close()


def test_path_escape_in_tampered_database_is_rejected(tmp_path: Path):
    source, database = prepared(tmp_path)
    controller = ReviewController(database, "Dominic")
    item = next(value for value in controller.pending() if value.source_path == "uncertain.txt")
    outside = tmp_path / "outside.txt"
    outside.write_bytes((source / item.source_path).read_bytes())
    database.execute("UPDATE review_sources SET relative_path='../outside.txt' WHERE source_id=?",
                     (item.source_id,))
    database.commit()
    escaped = next(value for value in controller.pending() if value.source_id == item.source_id)
    with pytest.raises(ValueError, match="escapes"):
        controller.accept(escaped, "REJECT", note="Tampered path")
    assert database.execute("SELECT COUNT(*) FROM archive_review_decisions").fetchone()[0] == 0
    database.close()
