from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class FileRecord:
    relative_path: str
    sha256: str
    size_bytes: int
    media_type: str
    modified_ns: int
    extracted_text: str | None = None
    extraction_status: str = "not_attempted"


@dataclass(frozen=True)
class Finding:
    kind: str
    severity: str
    message: str
    source_path: str
    evidence: str | None = None


@dataclass(frozen=True)
class Classification:
    module: str
    confidence: float
    labels: tuple[str, ...]
    summary: str
    source: str


@dataclass(frozen=True)
class ActionProposal:
    action: str
    source_path: str
    target: str | None
    reason: str
    authority_required: str
    status: str = "proposed"


@dataclass
class WorkbenchReport:
    contract_version: str
    root: str
    generated_at_utc: str
    files: list[FileRecord] = field(default_factory=list)
    duplicate_groups: list[list[str]] = field(default_factory=list)
    classifications: dict[str, Classification] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)
    proposals: list[ActionProposal] = field(default_factory=list)
    research_queue: list[dict[str, str]] = field(default_factory=list)
    recipient_routes: dict[str, list[str]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        for file_record in value["files"]:
            file_record.pop("extracted_text", None)
        return value
