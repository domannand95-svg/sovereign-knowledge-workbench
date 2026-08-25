from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import re


CONTRACT_VERSION = "sovereign.workbench.taxonomy.v1"
MAX_TAXONOMY_BYTES = 1024 * 1024
MAX_MODULES = 128
MAX_KEYWORDS = 256
MODULE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


@dataclass(frozen=True)
class Taxonomy:
    contract_version: str
    taxonomy_id: str
    fallback_module: str
    modules: dict[str, tuple[str, ...]]
    sha256: str


def load_taxonomy(path: Path) -> Taxonomy:
    raw = path.read_bytes()
    if len(raw) > MAX_TAXONOMY_BYTES:
        raise ValueError("Taxonomy contract exceeds the 1 MiB limit")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Taxonomy contract must be valid UTF-8 JSON") from exc
    if not isinstance(value, dict) or set(value) != {
        "contract_version", "taxonomy_id", "fallback_module", "modules"
    }:
        raise ValueError("Taxonomy contract has missing or unexpected top-level fields")
    if value["contract_version"] != CONTRACT_VERSION:
        raise ValueError(f"Unsupported taxonomy contract: {value['contract_version']!r}")
    taxonomy_id = value["taxonomy_id"]
    fallback = value["fallback_module"]
    modules = value["modules"]
    if not isinstance(taxonomy_id, str) or not MODULE_ID.fullmatch(taxonomy_id):
        raise ValueError("taxonomy_id is invalid")
    if not isinstance(fallback, str) or not MODULE_ID.fullmatch(fallback):
        raise ValueError("fallback_module is invalid")
    if not isinstance(modules, dict) or not 1 <= len(modules) <= MAX_MODULES:
        raise ValueError(f"modules must contain 1..{MAX_MODULES} entries")
    normalized: dict[str, tuple[str, ...]] = {}
    for module, terms in modules.items():
        if not isinstance(module, str) or not MODULE_ID.fullmatch(module):
            raise ValueError(f"Invalid module id: {module!r}")
        if not isinstance(terms, list) or not 1 <= len(terms) <= MAX_KEYWORDS:
            raise ValueError(f"Module {module!r} must contain 1..{MAX_KEYWORDS} keywords")
        keywords: list[str] = []
        for term in terms:
            if not isinstance(term, str) or not 1 <= len(term) <= 128 or term != term.strip():
                raise ValueError(f"Module {module!r} contains an invalid keyword")
            normalized_term = term.casefold()
            if normalized_term in keywords:
                raise ValueError(f"Module {module!r} contains a duplicate keyword")
            keywords.append(normalized_term)
        normalized[module] = tuple(keywords)
    return Taxonomy(CONTRACT_VERSION, taxonomy_id, fallback, normalized, sha256(raw).hexdigest())
