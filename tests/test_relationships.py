from __future__ import annotations

from sovereign_workbench.model import FileRecord
from sovereign_workbench.relationships import detect_relationships


def record(path: str, digest: str) -> FileRecord:
    return FileRecord(path, digest, 1, "text/plain", 0, "content", "extracted")


def types_for(relationships: list[dict[str, object]], source_id: str) -> set[str]:
    return {
        str(item["relationship_type"])
        for item in relationships if item["source_id"] == source_id
    }


def test_detects_exact_duplicate_by_digest():
    source = record("draft.docx", "a" * 64)
    duplicate = record("archive/draft copy.docx", "a" * 64)
    relationships = detect_relationships([("source", source)], [source, duplicate])
    assert types_for(relationships, "source") == {"EXACT_DUPLICATE"}


def test_detects_addendum_parent_and_version_siblings():
    addendum = record("Addendum to BA-RQS-001 v6.0.docx", "a" * 64)
    parent = record("BA-RQS-001 v6.0.docx", "b" * 64)
    updated = record("BA-RQS-001 v6.0 updated section 7.docx", "c" * 64)
    relationships = detect_relationships(
        [("addendum", addendum)], [addendum, parent, updated]
    )
    assert "POSSIBLE_PARENT" in types_for(relationships, "addendum")
    parent_candidates = [
        item for item in relationships if item["relationship_type"] == "POSSIBLE_PARENT"
    ]
    assert parent_candidates[0]["related_relative_path"] == "BA-RQS-001 v6.0.docx"
    assert not any(
        item["relationship_type"] == "VERSION_SIBLING"
        and item["related_relative_path"] == "BA-RQS-001 v6.0.docx"
        for item in relationships
    )


def test_detects_genuine_version_siblings():
    section_seven = record("BA-RQS-001 v6.0 section 7.docx", "a" * 64)
    section_eight = record("BA-RQS-001 v6.0 section 8.docx", "b" * 64)
    relationships = detect_relationships(
        [("section-seven", section_seven)], [section_seven, section_eight]
    )
    assert types_for(relationships, "section-seven") == {"VERSION_SIBLING"}


def test_detects_updated_supersession_and_hash_companion():
    updated = record("BA-RQS-001 v6.0 updated.docx", "a" * 64)
    base = record("BA-RQS-001 v6.0.docx", "b" * 64)
    hashed = record("BA-RQS-001 v6.0 hash.docx", "c" * 64)
    relationships = detect_relationships(
        [("updated", updated), ("hashed", hashed)], [updated, base, hashed]
    )
    assert "POSSIBLE_SUPERSEDES" in types_for(relationships, "updated")
    assert "HASH_COMPANION" in types_for(relationships, "hashed")


def test_unrelated_files_produce_no_relationships():
    source = record("aerospace.docx", "a" * 64)
    unrelated = record("personal shopping list.docx", "b" * 64)
    assert detect_relationships([("source", source)], [source, unrelated]) == []


def test_hash_companion_requires_all_non_hash_identity_tokens_to_match():
    source = record("Addendum to BA-RQS-001 v6.0.docx", "a" * 64)
    different_section = record("Addendum to BA-RQS-001 v6.0 section 8 hash.docx", "b" * 64)
    relationships = detect_relationships(
        [("source", source)], [source, different_section]
    )
    assert "HASH_COMPANION" not in types_for(relationships, "source")
