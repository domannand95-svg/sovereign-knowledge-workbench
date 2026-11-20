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

    # The rules compare every selected file with every corpus path. Each path's stem,
    # tokens and marker words are computed once per call instead of once per comparison;
    # the rules and their results are unchanged.
    stems: dict[str, str] = {}
    tokens: dict[tuple[str, frozenset[str]], frozenset[str]] = {}
    words: dict[tuple[str, str], bool] = {}
    versions: dict[str, re.Match[str] | None] = {}

    def stem(path: str) -> str:
        if path not in stems:
            stems[path] = _stem(path)
        return stems[path]

    def tokens_of(path: str, ignored: frozenset[str]) -> frozenset[str]:
        key = (path, ignored)
        if key not in tokens:
            tokens[key] = frozenset(word for word in WORDS.findall(stem(path)) if word not in ignored)
        return tokens[key]

    def has_word(path: str, word: str) -> bool:
        key = (path, word)
        if key not in words:
            words[key] = bool(re.search(rf"(?<!\w){re.escape(word)}(?!\w)", stem(path)))
        return words[key]

    def version_of(path: str) -> re.Match[str] | None:
        if path not in versions:
            versions[path] = VERSION.search(stem(path))
        return versions[path]

    def similarity(left: str, right: str, ignored: frozenset[str], containment: bool = True) -> float:
        left_tokens = tokens_of(left, ignored)
        right_tokens = tokens_of(right, ignored)
        if not left_tokens or not right_tokens:
            return 0.0
        intersection = len(left_tokens & right_tokens)
        jaccard = intersection / len(left_tokens | right_tokens)
        containment_score = intersection / min(len(left_tokens), len(right_tokens))
        return max(jaccard, containment_score) if containment else jaccard

    stop = frozenset(STOP_WORDS)
    parent_ignored = frozenset(STOP_WORDS | PARENT_MARKERS)
    supersede_ignored = frozenset(STOP_WORDS | {"revised", "updated"})
    hash_ignored = frozenset(STOP_WORDS | {"hash"})
    by_digest: dict[str, list[FileRecord]] = {}
    by_identity: dict[frozenset[str], list[FileRecord]] = {}
    for item in corpus:
        if item.sha256:
            by_digest.setdefault(item.sha256, []).append(item)
        by_identity.setdefault(tokens_of(item.relative_path, hash_ignored), []).append(item)

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
        path = source.relative_path
        others = [item for item in corpus if item.relative_path != path]
        parent_path: str | None = None
        if source.sha256:
            for related in by_digest.get(source.sha256, ()):
                if related.relative_path != path:
                    add(source_id, "EXACT_DUPLICATE", related, 1.0,
                        "Source and related file have the same SHA-256 digest")

        if has_word(path, "addendum"):
            candidates = [
                (similarity(path, related.relative_path, parent_ignored), related)
                for related in others if not has_word(related.relative_path, "addendum")
            ]
            candidates = [item for item in candidates if item[0] >= 0.45]
            if candidates:
                score, related = max(candidates, key=lambda item: (item[0], item[1].relative_path))
                parent_path = related.relative_path
                add(source_id, "POSSIBLE_PARENT", related, score,
                    "Addendum marker and normalized filename-token similarity")

        source_version = version_of(path)
        if source_version:
            for related in others:
                if related.relative_path == parent_path:
                    continue
                related_version = version_of(related.relative_path)
                score = similarity(path, related.relative_path, stop, containment=False)
                if (related_version and source_version.group().casefold() == related_version.group().casefold()
                        and score >= 0.6):
                    add(source_id, "VERSION_SIBLING", related, score,
                        f"Shared version marker {source_version.group()} and normalized filename similarity")

        if has_word(path, "revised") or has_word(path, "updated"):
            candidates = [
                (similarity(path, related.relative_path, supersede_ignored, containment=False), related)
                for related in others
                if not has_word(related.relative_path, "revised")
                and not has_word(related.relative_path, "updated")
            ]
            candidates = [item for item in candidates if item[0] >= 0.8]
            if candidates:
                score, related = max(candidates, key=lambda item: (item[0], item[1].relative_path))
                add(source_id, "POSSIBLE_SUPERSEDES", related, score,
                    "Updated/revised marker and normalized filename-token similarity")

        # Only paths with the same identity tokens can be hash companions.
        source_hash_marker = has_word(path, "hash")
        for related in by_identity.get(tokens_of(path, hash_ignored), ()):
            if related.relative_path != path and source_hash_marker != has_word(related.relative_path, "hash"):
                add(source_id, "HASH_COMPANION", related, 1.0,
                    "Exactly one filename has a hash marker and remaining identity tokens match")

    return sorted(relationships, key=lambda item: (
        str(item["source_id"]), str(item["relationship_type"]),
        str(item["related_relative_path"]),
    ))
