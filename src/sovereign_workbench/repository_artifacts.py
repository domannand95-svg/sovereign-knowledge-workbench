from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


RELATIONSHIPS = {
    "SUPERSEDED_REPOSITORY_COPY", "REPOSITORY_PROVENANCE_COPY", "CANONICAL_MATCH",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def route_repository_artifact(
    database: sqlite3.Connection, *, source_id: str, source_sha256: str,
    repository_name: str, canonical_path: Path, relationship: str,
    reviewer: str, reason: str,
) -> str:
    if relationship not in RELATIONSHIPS:
        raise ValueError("Invalid repository artifact relationship")
    if not repository_name.strip() or not reviewer.strip() or not reason.strip():
        raise ValueError("Repository, reviewer, and reason are required")
    source = database.execute(
        "SELECT source_sha256 FROM review_sources WHERE source_id=?", (source_id,)
    ).fetchone()
    if not source or source[0] != source_sha256:
        raise ValueError("Archive source hash mismatch")
    resolved = canonical_path.resolve(strict=True)
    canonical_sha256 = _file_hash(resolved)
    value = {"contract_version": "sovereign.workbench.repository-artifact-route.v1",
             "source_id": source_id, "source_sha256": source_sha256,
             "repository_name": repository_name.strip(), "canonical_path": str(resolved),
             "canonical_sha256": canonical_sha256, "relationship": relationship,
             "reason": reason.strip(), "dataset_disposition": "EXCLUDED",
             "movement_authority": "NOT_AUTHORIZED", "reviewer": reviewer.strip(),
             "authority": "NONE"}
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    route_id = f"RAR-{digest[:24]}"
    database.execute(
        "INSERT OR IGNORE INTO archive_repository_artifact_routes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (route_id, source_id, source_sha256, repository_name.strip(), str(resolved),
         canonical_sha256, relationship, reason.strip(), "EXCLUDED", "NOT_AUTHORIZED",
         reviewer.strip(), digest, "NONE", _now()))
    database.commit()
    return route_id
