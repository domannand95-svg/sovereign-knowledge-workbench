from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .family_review import _family_key
from .review_batches import BEHAVIOR_CATEGORIES


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _table_exists(database: sqlite3.Connection, name: str) -> bool:
    return database.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def audit_family_lineage(database: sqlite3.Connection) -> dict[str, object]:
    """Build a read-only, revision-safe lineage and dataset-readiness audit."""
    rows = database.execute(
        "SELECT s.source_id,s.relative_path,s.source_sha256,s.modified_ns,"
        "COALESCE(c.proposed_topic,'Needs review'),COALESCE(c.proposed_maturity,'Needs review') "
        "FROM review_sources s LEFT JOIN archive_review_candidates c ON c.source_id=s.source_id "
        "ORDER BY s.relative_path,s.source_id"
    ).fetchall()
    sources = {row[0]: row for row in rows}
    parent = {source_id: source_id for source_id in sources}

    def find(value: str) -> str:
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(left: str, right: str) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    by_hash: dict[str, list[str]] = defaultdict(list)
    by_key: dict[str, list[str]] = defaultdict(list)
    for source_id, path, digest, _modified, topic, _maturity in rows:
        by_hash[digest].append(source_id)
        by_key[_family_key(path, topic)].append(source_id)
    for group in by_hash.values():
        for source_id in group[1:]:
            union(group[0], source_id)
    # Filename/topic clusters are conservative leakage boundaries, not assertions of identity.
    for key, group in by_key.items():
        if key.endswith(":unresolved"):
            continue
        for source_id in group[1:]:
            union(group[0], source_id)

    explicit_relationships: list[dict[str, object]] = []
    path_index = {row[1]: row[0] for row in rows}
    if _table_exists(database, "archive_review_relationships"):
        for source_id, relationship, related_path, confidence, evidence in database.execute(
            "SELECT source_id,relationship_type,related_relative_path,confidence,evidence "
            "FROM archive_review_relationships ORDER BY source_id,related_relative_path"
        ):
            related_id = path_index.get(related_path)
            if related_id and relationship != "HASH_COMPANION":
                union(source_id, related_id)
            explicit_relationships.append({"source_id": source_id,
                "related_source_id": related_id, "related_relative_path": related_path,
                "relationship": relationship, "confidence": confidence, "evidence": evidence})
    if _table_exists(database, "archive_document_family_members"):
        for _family_id, members in _group_rows(database.execute(
            "SELECT family_id,source_id FROM archive_document_family_members ORDER BY family_id,source_id"
        ).fetchall()).items():
            for source_id in members[1:]:
                union(members[0], source_id)

    latest_source = {row[0]: row[1:] for row in database.execute(
        "WITH ranked AS (SELECT source_id,decision,privacy_status,canonical_status,decided_at,"
        "ROW_NUMBER() OVER(PARTITION BY source_id ORDER BY decided_at DESC,decision_id DESC) rank "
        "FROM archive_review_decisions) SELECT source_id,decision,privacy_status,canonical_status,decided_at "
        "FROM ranked WHERE rank=1"
    )}
    latest_maps = {row[0]: row[1] for row in database.execute(
        "WITH ranked AS (SELECT source_id,decision,ROW_NUMBER() OVER(PARTITION BY source_id "
        "ORDER BY decided_at DESC,decision_id DESC) rank FROM archive_document_map_decisions) "
        "SELECT source_id,decision FROM ranked WHERE rank=1"
    )}
    excluded = {row[0] for row in database.execute(
        "SELECT DISTINCT x.source_id FROM archive_section_exclusions e "
        "JOIN archive_document_sections x ON x.section_id=e.section_id"
    )}
    routed = {row[0] for row in database.execute(
        "SELECT DISTINCT source_id FROM archive_repository_artifact_routes"
    )}

    components: dict[str, list[str]] = defaultdict(list)
    for source_id in sources:
        components[find(source_id)].append(source_id)
    exact_duplicates = []
    for digest, group in sorted(by_hash.items()):
        if len(group) > 1:
            exact_duplicates.append({"source_sha256": digest,
                                     "source_ids": sorted(group), "count": len(group)})

    contradictory = Counter()
    if _table_exists(database, "archive_family_claim_coverage"):
        for family_id, count in database.execute(
            "SELECT c.family_id,COUNT(DISTINCT c.claim_id) FROM archive_family_claims c "
            "WHERE EXISTS (SELECT 1 FROM archive_family_claim_coverage a WHERE a.claim_id=c.claim_id "
            "AND a.stance='ASSERTS') AND EXISTS (SELECT 1 FROM archive_family_claim_coverage n "
            "WHERE n.claim_id=c.claim_id AND n.stance='CONTRADICTS') GROUP BY c.family_id"
        ):
            contradictory[family_id] = count

    families = []
    for members in sorted(components.values(), key=lambda value: min(value)):
        member_rows = [sources[source_id] for source_id in members]
        family_digest = hashlib.sha256("\n".join(sorted(row[2] for row in member_rows)).encode()).hexdigest()
        family_id = f"AUD-FAM-{family_digest[:24]}"
        ranked = sorted(member_rows, key=lambda row: (
            latest_source.get(row[0], ("", "", "", ""))[0] == "APPROVE",
            latest_maps.get(row[0]) == "MAP_ACCEPTED",
            row[0] not in excluded and row[0] not in routed,
            row[3], row[1]), reverse=True)
        canonical = ranked[0]
        families.append({"family_id": family_id,
            "boundary_kind": "CONSERVATIVE_LEAKAGE_BOUNDARY",
            "member_count": len(members), "source_ids": sorted(members),
            "canonical_candidate": canonical[0],
            "canonical_candidate_path": canonical[1],
            "canonical_status": "CANDIDATE_NOT_AUTHORIZED",
            "topics": sorted({row[4] for row in member_rows}),
            "maturities": sorted({row[5] for row in member_rows})})

    allocation_rows = database.execute(
        "SELECT source_id,source_family_id,allocation,behavior_category FROM archive_dataset_allocations"
    ).fetchall()
    split_by_family: dict[str, set[str]] = defaultdict(set)
    category_counts = Counter()
    allocation_counts = Counter()
    for _source_id, family_id, allocation, category in allocation_rows:
        allocation_counts[allocation] += 1
        category_counts[category] += 1
        if allocation in {"TRAIN_ELIGIBLE", "HELD_OUT_LOCKED"}:
            split_by_family[family_id].add(allocation)
    family_leaks = sorted(family_id for family_id, splits in split_by_family.items() if len(splits) > 1)
    eligible = [source_id for source_id, decision in latest_source.items()
                if decision[0] == "APPROVE" and decision[1] == "Reviewed"
                and source_id not in excluded and source_id not in routed]
    train_count = allocation_counts["TRAIN_ELIGIBLE"]
    held_out_count = allocation_counts["HELD_OUT_LOCKED"]
    missing_categories = sorted(category for category in BEHAVIOR_CATEGORIES
                                if category_counts[category] < 25)
    blockers = []
    if train_count < 300:
        blockers.append(f"TRAIN_ELIGIBLE shortfall: {300 - train_count}")
    if held_out_count < 100:
        blockers.append(f"HELD_OUT_LOCKED shortfall: {100 - held_out_count}")
    if missing_categories:
        blockers.append("Critical behavior categories below 25 examples: " + ", ".join(missing_categories))
    if family_leaks:
        blockers.append("Family leakage detected")
    if len(rows) < 400:
        blockers.append(f"Source-level contract capacity is {len(rows)}; 400 requires {400-len(rows)} more examples")

    return {"contract_version": "sovereign.workbench.family-dataset-audit.v1",
            "generated_at": _now(), "authority": "NONE", "training_authorized": False,
            "source_count": len(rows), "family_count": len(families),
            "families": families, "exact_duplicates": exact_duplicates,
            "explicit_relationships": explicit_relationships,
            "superseded_candidates": [item for item in explicit_relationships
                                      if item["relationship"] == "POSSIBLE_SUPERSEDES"],
            "contradictory_claims_by_legacy_family": dict(sorted(contradictory.items())),
            "clean_canonical_candidate_count": len(eligible),
            "clean_canonical_candidate_source_ids": sorted(eligible),
            "dataset_audit": {"allocation_counts": dict(sorted(allocation_counts.items())),
                "behavior_category_counts": {key: category_counts[key] for key in sorted(BEHAVIOR_CATEGORIES)},
                "family_leaks": family_leaks, "train_manifest_valid": train_count >= 300 and not family_leaks,
                "held_out_manifest_valid": held_out_count >= 100 and not family_leaks,
                "ready_for_fine_tuning": not blockers, "blockers": blockers}}


def _group_rows(rows: list[tuple[str, str]]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = defaultdict(list)
    for key, value in rows:
        grouped[key].append(value)
    return grouped


def export_family_audit(database: sqlite3.Connection, state_db: Path, output: Path) -> dict[str, object]:
    if output.exists():
        raise ValueError("Family audit output already exists")
    before = _file_sha256(state_db)
    value = audit_family_lineage(database)
    after = _file_sha256(state_db)
    if before != after:
        raise ValueError("Review database changed during read-only family audit")
    value["state_db_sha256"] = before
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    value["audit_sha256"] = hashlib.sha256(encoded.encode()).hexdigest()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                      encoding="utf-8", newline="\n")
    return value
