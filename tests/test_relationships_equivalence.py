"""The cached relationship detector must give exactly the original detector's output.

The original (f790dff) compared every selected file with every corpus path and rebuilt
each path's stem and tokens per comparison: ~90% of a 1,000-file import of a 4,100-file
archive (2026-11-20 profile). It is kept here verbatim as the reference.
"""
import random
import re
from pathlib import PurePath

from sovereign_workbench.model import FileRecord
from sovereign_workbench.relationships import detect_relationships

VERSION = re.compile(r"\bv\d+(?:\.\d+)*\b", re.IGNORECASE)
WORDS = re.compile(r"[a-z0-9]+")
STOP_WORDS = {"a", "an", "and", "copy", "document", "draft", "final", "of", "the", "to"}
PARENT_MARKERS = {"addendum", "hash", "revised", "section", "updated"}


def _stem(path):
    return PurePath(path.replace("\\", "/")).stem.casefold()


def _tokens(path, ignored=None):
    ignored = STOP_WORDS if ignored is None else ignored
    return {word for word in WORDS.findall(_stem(path)) if word not in ignored}


def _similarity(left, right, *, ignored=None, containment=True):
    left_tokens, right_tokens = _tokens(left, ignored), _tokens(right, ignored)
    if not left_tokens or not right_tokens:
        return 0.0
    intersection = len(left_tokens & right_tokens)
    jaccard = intersection / len(left_tokens | right_tokens)
    containment_score = intersection / min(len(left_tokens), len(right_tokens))
    return max(jaccard, containment_score) if containment else jaccard


def _has_word(path, word):
    return bool(re.search(rf"(?<!\w){re.escape(word)}(?!\w)", _stem(path)))


def reference(selected, corpus):
    relationships, seen = [], set()

    def add(source_id, relation, related, confidence, evidence):
        key = (source_id, relation, related.relative_path)
        if key in seen:
            return
        seen.add(key)
        relationships.append({"source_id": source_id, "relationship_type": relation,
                              "related_relative_path": related.relative_path,
                              "related_sha256": related.sha256, "confidence": round(confidence, 4),
                              "evidence": evidence})

    for source_id, source in selected:
        others = [item for item in corpus if item.relative_path != source.relative_path]
        parent_path = None
        for related in others:
            if source.sha256 and source.sha256 == related.sha256:
                add(source_id, "EXACT_DUPLICATE", related, 1.0, "Source and related file have the same SHA-256 digest")
        if _has_word(source.relative_path, "addendum"):
            candidates = [(_similarity(source.relative_path, related.relative_path,
                                       ignored=STOP_WORDS | PARENT_MARKERS), related)
                          for related in others if not _has_word(related.relative_path, "addendum")]
            candidates = [item for item in candidates if item[0] >= 0.45]
            if candidates:
                score, related = max(candidates, key=lambda item: (item[0], item[1].relative_path))
                parent_path = related.relative_path
                add(source_id, "POSSIBLE_PARENT", related, score, "Addendum marker and normalized filename-token similarity")
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
            candidates = [(_similarity(source.relative_path, related.relative_path,
                                       ignored=STOP_WORDS | {"revised", "updated"}, containment=False), related)
                          for related in others
                          if not _has_word(related.relative_path, "revised") and not _has_word(related.relative_path, "updated")]
            candidates = [item for item in candidates if item[0] >= 0.8]
            if candidates:
                score, related = max(candidates, key=lambda item: (item[0], item[1].relative_path))
                add(source_id, "POSSIBLE_SUPERSEDES", related, score,
                    "Updated/revised marker and normalized filename-token similarity")
        for related in others:
            one_hash_marker = _has_word(source.relative_path, "hash") != _has_word(related.relative_path, "hash")
            if one_hash_marker and _tokens(source.relative_path, STOP_WORDS | {"hash"}) == _tokens(
                    related.relative_path, STOP_WORDS | {"hash"}):
                add(source_id, "HASH_COMPANION", related, 1.0,
                    "Exactly one filename has a hash marker and remaining identity tokens match")
    return sorted(relationships, key=lambda item: (str(item["source_id"]), str(item["relationship_type"]),
                                                   str(item["related_relative_path"])))


PARTS = ["report", "plan", "budget", "notes", "spec", "site", "trial", "a", "the", "copy", "final", "draft",
         "addendum", "revised", "updated", "hash", "section", "v1", "v2", "v1.3", "V2", "2020", "Résumé"]


def test_cached_detector_matches_reference_on_a_random_corpus():
    rng = random.Random(20261120)
    corpus = []
    for index in range(600):
        name = " ".join(rng.choice(PARTS) for _ in range(rng.randint(1, 4)))
        folder = rng.choice(["", "a/", "b/c/", "Docs\\"])
        digest = f"{rng.randint(0, 120):064d}" if rng.random() < 0.8 else ""
        corpus.append(FileRecord(f"{folder}{name}-{index % 50}.{rng.choice(['md', 'txt', 'docx'])}",
                                 digest, 1, "x", 0))
    selected = [(f"id-{index}", record) for index, record in enumerate(rng.sample(corpus, 200))]
    expected = reference(selected, corpus)
    assert {item["relationship_type"] for item in expected} >= {
        "EXACT_DUPLICATE", "POSSIBLE_PARENT", "VERSION_SIBLING", "POSSIBLE_SUPERSEDES", "HASH_COMPANION"}
    assert detect_relationships(selected, corpus) == expected
