"""Read-only search over admitted archive sources.

Text is read from the original files on demand and searched only after the exact bytes
read have been matched to the SHA-256 recorded at intake. No copy of document text is
stored. A source that is missing, changed or unreadable is reported, never silently
dropped, and its current contents are never presented as the admitted evidence.

Search results carry no authority. A recorded review decision is shown as context; it
is not permission to move, publish, export or disclose anything. A hash match shows that
the content is what was admitted, not that its claims are true.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path, PurePosixPath

from .extract import extract_text_from_bytes
from .review_batches import LATEST_DECISION_ORDER

CONTRACT_VERSION = "sovereign.workbench.archive-search.v1"
MAX_QUERY_CHARS = 500
MAX_LINE_CHARS = 300
MAX_LINES_PER_SOURCE = 20
REQUIRED_TABLES = {"archive_intake_roots", "review_sources", "review_batch_items",
                   "archive_review_candidates", "archive_review_decisions"}

VERIFIED = "VERIFIED"
CHANGED = "CHANGED_SINCE_INTAKE"
MISSING = "MISSING"
NOT_REGULAR = "NOT_A_REGULAR_FILE"
OUTSIDE_ROOT = "OUTSIDE_INTAKE_ROOT"
TOO_LARGE = "TOO_LARGE_TO_SEARCH"
UNREADABLE = "UNREADABLE"
NO_TEXT = "NO_SEARCHABLE_TEXT"

METADATA_FIELDS = ("relative_path", "model_summary", "topic", "review_note", "research_question")


def connect_readonly(path: Path) -> sqlite3.Connection:
    """Open the review database without any write, schema or journal-mode change."""
    if not Path(path).is_file():
        raise ValueError("The review database does not exist; nothing has been admitted")
    try:
        database = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)
        tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    except sqlite3.Error as exc:
        raise ValueError(f"The review database could not be opened read-only: {exc}") from exc
    if not REQUIRED_TABLES <= tables:
        database.close()
        raise ValueError("The review database has no archive review tables")
    return database


def _latest_decisions(database: sqlite3.Connection) -> dict[str, dict[str, str]]:
    latest: dict[str, dict[str, str]] = {}
    for row in database.execute(
        "SELECT source_id,decision,topic,maturity,review_note,research_question,reviewer,decided_at "
        f"FROM archive_review_decisions ORDER BY source_id,{LATEST_DECISION_ORDER}"
    ):
        latest.setdefault(row[0], {
            "decision": row[1], "topic": row[2], "maturity": row[3], "review_note": row[4],
            "research_question": row[5], "reviewer": row[6], "decided_at": row[7],
        })
    return latest


def _safe_relative(relative: str) -> bool:
    pure = PurePosixPath(relative)
    return (bool(relative) and not pure.is_absolute() and "\\" not in relative
            and all(part not in ("", ".", "..") and ":" not in part for part in pure.parts))


def _has_link(root: Path, relative: str) -> bool:
    current = root
    for part in PurePosixPath(relative).parts:
        current = current / part
        if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
            return True
    return False


def _read_verified(root: str, relative: str, expected_sha256: str,
                   max_bytes: int) -> tuple[str, bytes | None]:
    if not _safe_relative(relative):
        return OUTSIDE_ROOT, None
    base = Path(root)
    path = base.joinpath(*PurePosixPath(relative).parts)
    try:
        if _has_link(base, relative):
            return NOT_REGULAR, None
        if not path.exists():
            return MISSING, None
        if not path.is_file():
            return NOT_REGULAR, None
        if not path.resolve().is_relative_to(base.resolve()):
            return OUTSIDE_ROOT, None
        if path.stat().st_size > max_bytes:
            return TOO_LARGE, None
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            data = stream.read(max_bytes + 1)
    except OSError:
        return UNREADABLE, None
    if len(data) > max_bytes:
        return TOO_LARGE, None
    # The hash is the integrity guarantee: whatever happened above, only bytes that
    # match the intake identity are ever searched or shown.
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        return CHANGED, None
    return VERIFIED, data


def _lines(text: str) -> list[str]:
    # Split on newlines only, so line numbers match what an editor shows.
    return [line[:-1] if line.endswith("\r") else line for line in text.split("\n")]


def _snippet(line: str, start: int, length: int) -> str:
    if len(line) <= MAX_LINE_CHARS:
        return line.strip()
    left = max(0, min(start - (MAX_LINE_CHARS - length) // 2, len(line) - MAX_LINE_CHARS))
    window = line[left:left + MAX_LINE_CHARS]
    return ("…" if left else "") + window + ("…" if left + MAX_LINE_CHARS < len(line) else "")


def search_archive(database: sqlite3.Connection, query: str, *, limit: int = 50,
                   max_file_bytes: int = 20 * 1024 * 1024) -> dict[str, object]:
    needle = query.strip()
    if not needle or len(needle) > MAX_QUERY_CHARS or any(ord(c) < 0x20 for c in needle):
        raise ValueError(f"Search text must be 1-{MAX_QUERY_CHARS} printable characters")
    if limit < 1 or limit > 500:
        raise ValueError("Search limit must be between 1 and 500")
    folded = needle.casefold()
    decisions = _latest_decisions(database)
    roots = {row[0]: row[1] for row in database.execute(
        "SELECT canonical_root,root_id FROM archive_intake_roots")}
    rows = database.execute(
        "SELECT s.source_id,s.root,s.relative_path,s.source_sha256,"
        "COALESCE(i.batch_id,''),COALESCE(c.model_summary,'') FROM review_sources s "
        "LEFT JOIN review_batch_items i ON i.source_id=s.source_id "
        "LEFT JOIN archive_review_candidates c ON c.source_id=s.source_id "
        "ORDER BY s.root,s.relative_path,s.source_id"
    ).fetchall()

    results: list[dict[str, object]] = []
    unverified: list[dict[str, str]] = []
    not_searchable: list[dict[str, str]] = []
    matching_sources = 0
    # Every source is verified even after the result limit is reached, so the lists of
    # unverified and unsearchable sources are always complete.
    for source_id, root, relative, sha256, batch_id, summary in rows:
        decision = decisions.get(source_id)
        status, data = _read_verified(root, relative, sha256, max_file_bytes)
        content_matches: list[dict[str, object]] = []
        capped = False
        searched = False
        if status == VERIFIED:
            suffix = PurePosixPath(relative).suffix.casefold()
            # Bytes are capped by max_file_bytes, so the whole verified text is searched.
            text, _ = extract_text_from_bytes(data or b"", suffix, max_chars=len(data or b"") + 1)
            if text is None:
                not_searchable.append({"source_id": source_id, "relative_path": relative,
                                       "intake_root": root, "status": NO_TEXT})
            else:
                searched = True
                if text.startswith("﻿"):
                    text = text[1:]
                for number, line in enumerate(_lines(text), start=1):
                    at = line.casefold().find(folded)
                    if at >= 0:
                        if len(content_matches) >= MAX_LINES_PER_SOURCE:
                            capped = True
                            break
                        content_matches.append({"line": number,
                                                "text": _snippet(line, at, len(needle))})
        else:
            unverified.append({"source_id": source_id, "relative_path": relative,
                               "intake_root": root, "status": status})

        metadata = {"relative_path": relative, "model_summary": summary}
        if decision:
            metadata.update({key: decision[key] for key in
                             ("topic", "review_note", "research_question")})
        metadata_matches = [field for field in METADATA_FIELDS
                            if folded in str(metadata.get(field, "")).casefold()]

        if not content_matches and not metadata_matches:
            continue
        matching_sources += 1
        if len(results) >= limit:
            continue
        results.append({
            "source_id": source_id,
            "relative_path": relative,
            "intake_root": root,
            "intake_root_id": roots.get(root, ""),
            "admitted_sha256": sha256,
            "batch_id": batch_id,
            "verification": status,
            "content_verified": status == VERIFIED,
            "content_searched": searched,
            "content_matches": content_matches,
            "content_matches_capped": capped,
            "metadata_matches": metadata_matches,
            "latest_decision": ({key: decision[key] for key in
                                 ("decision", "topic", "maturity", "reviewer", "decided_at",
                                  "review_note")} if decision else None),
        })

    return {
        "contract_version": CONTRACT_VERSION,
        "authority": "NONE",
        "query": needle,
        "sources_considered": len(rows),
        "matching_sources": matching_sources,
        "results": results,
        "truncated": matching_sources > len(results),
        "unverified_sources": unverified,
        "not_searchable_sources": not_searchable,
        "note": ("Content matches are lines from files whose bytes match the intake hash; a "
                 "match shows the text is what was admitted, not that it is true. "
                 "metadata_matches only names recorded fields (file name, generated summary, "
                 "review notes) that contain the search text; their text is not quoted. A review "
                 "decision is not authorization to move, export or disclose a document."),
    }
