from __future__ import annotations

import re
from pathlib import PurePath

from .model import FileRecord


VERSION = re.compile(r"\bv\d+(?:\.\d+)*\b", re.IGNORECASE)
WORDS = re.compile(r"[a-z0-9]+")
STOP_WORDS = {"a", "an", "and", "copy", "document", "draft", "final", "of", "the", "to"}
PARENT_MARKERS = {"addendum", "hash", "revised", "section", "updated"}


def _stem(path: str) -> str:
    return PurePath(path.replace("\\", "/")).stem.casefold()


def _tokens(path: str, ignored: set[str] | None = None) -> set[str]:
    ignored = STOP_WORDS if ignored is None else ignored
    return {word for word in WORDS.findall(_stem(path)) if word not in ignored}


def _similarity(left: str, right: str, *, ignored: set[str] | None = None,
                containment: bool = True) -> float:
    left_tokens = _tokens(left, ignored)
    right_tokens = _tokens(right, ignored)
    if not left_tokens or not right_tokens:
        return 0.0
    intersection = len(left_tokens & right_tokens)
    jaccard = intersection / len(left_tokens | right_tokens)
    containment_score = intersection / min(len(left_tokens), len(right_tokens))
    return max(jaccard, containment_score) if containment else jaccard


def _has_word(path: str, word: str) -> bool:
    return bool(re.search(rf"(?<!\w){re.escape(word)}(?!\w)", _stem(path)))


def detect_relationships(selected: list[tuple[str, FileRecord]],
                         corpus: list[FileRecord]) -> list[dict[str, object]]:
    relationships: list[dict[str, object]] = []
    seen: set[tuple[str, str, str]] = set()

    def add(source_id: str, relation: str, related: FileRecord,
            confidence: float, evidence: str) -> None:
        key = (source_id, relation, related.relative_path)
        if key in seen:
            return
        seen.add(key)
        relationships.append({
            "source_id": source_id,
            "relationship_type": relation,
            "related_relative_path": related.relative_path,
            "related_sha256": related.sha256,
            "confidence": round(confidence, 4),
            "evidence": evidence,
        })

    for source_id, source in selected:
        others = [item for item in corpus if item.relative_path != source.relative_path]
        parent_path: str | None = None
        for related in others:
            if source.sha256 and source.sha256 == related.sha256:
                add(source_id, "EXACT_DUPLICATE", related, 1.0,
                    "Source and related file have the same SHA-256 digest")

        if _has_word(source.relative_path, "addendum"):
            candidates = [
                (_similarity(source.relative_path, related.relative_path,
                             ignored=STOP_WORDS | PARENT_MARKERS), related)
                for related in others if not _has_word(related.relative_path, "addendum")
            ]
            candidates = [item for item in candidates if item[0] >= 0.45]
            if candidates:
                score, related = max(candidates, key=lambda item: (item[0], item[1].relative_path))
                parent_path = related.relative_path
                add(source_id, "POSSIBLE_PARENT", related, score,
                    "Addendum marker and normalized filename-token similarity")

        source_version = VERSION.search(_stem(source.relative_path))
        if source_version:
            for related in others:
                if related.relative_path == parent_path:
                    continue
                related_version = VERSION.search(_stem(related.relative_path))
                score = _similarity(source.relative_path, related.relative_path, containment=False)
                if (related_version and source_version.group().casefold() == related_version.group().casefold()
                        and score >= 0.6):
                    add(source_id, "VERSION_SIBLING", related, score,
                        f"Shared version marker {source_version.group()} and normalized filename similarity")

        if _has_word(source.relative_path, "revised") or _has_word(source.relative_path, "updated"):
            candidates = [
                (_similarity(source.relative_path, related.relative_path,
                             ignored=STOP_WORDS | {"revised", "updated"},
                             containment=False), related)
                for related in others
                if not _has_word(related.relative_path, "revised")
                and not _has_word(related.relative_path, "updated")
            ]
            candidates = [item for item in candidates if item[0] >= 0.8]
            if candidates:
                score, related = max(candidates, key=lambda item: (item[0], item[1].relative_path))
                add(source_id, "POSSIBLE_SUPERSEDES", related, score,
                    "Updated/revised marker and normalized filename-token similarity")

        for related in others:
            one_hash_marker = _has_word(source.relative_path, "hash") != _has_word(
                related.relative_path, "hash"
            )
            source_identity = _tokens(source.relative_path, STOP_WORDS | {"hash"})
            related_identity = _tokens(related.relative_path, STOP_WORDS | {"hash"})
            if one_hash_marker and source_identity == related_identity:
                add(source_id, "HASH_COMPANION", related, 1.0,
                    "Exactly one filename has a hash marker and remaining identity tokens match")

    return sorted(relationships, key=lambda item: (
        str(item["source_id"]), str(item["relationship_type"]),
        str(item["related_relative_path"]),
    ))
