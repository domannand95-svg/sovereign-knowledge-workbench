from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .model import WorkbenchReport


def canonical_json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def build_review_package(report: WorkbenchReport) -> dict:
    payload = report.to_dict()
    payload_bytes = canonical_json(payload)
    return {
        "contract_version": "sovereign.workbench.review-package.v1",
        "report_sha256": hashlib.sha256(payload_bytes).hexdigest(),
        "report": payload,
        "authority": "review_only",
        "external_dispatch": "not_authorized",
    }


def write_review_package(package: dict, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_bytes(canonical_json(package))
    temporary.replace(output)
