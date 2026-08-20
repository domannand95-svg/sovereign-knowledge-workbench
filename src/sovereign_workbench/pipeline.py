from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .analysis import classify, privacy_findings, research_gaps
from .intake import duplicate_groups, scan_files
from .local_model import LocalModelConfig, ModelError, classify_with_local_model
from .model import ActionProposal, Finding, WorkbenchReport
from .routing import load_routes, route_candidate


def analyze_workspace(
    root: Path,
    *,
    routes_path: Path | None = None,
    use_local_model: bool = False,
    max_file_bytes: int = 50 * 1024 * 1024,
) -> WorkbenchReport:
    records = scan_files(root, max_file_bytes=max_file_bytes)
    report = WorkbenchReport(
        contract_version="sovereign.workbench.report.v1",
        root=str(root.resolve()),
        generated_at_utc=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        files=records,
        duplicate_groups=duplicate_groups(records),
    )
    routes = load_routes(routes_path)
    model_config = LocalModelConfig.from_environment()

    duplicate_paths = {path for group in report.duplicate_groups for path in group[1:]}
    for record in records:
        classification = classify(record)
        if use_local_model and record.extracted_text:
            try:
                classification = classify_with_local_model(record, model_config)
            except ModelError as exc:
                report.findings.append(Finding(
                    "model_failure", "medium", str(exc), record.relative_path,
                ))
        report.classifications[record.relative_path] = classification
        file_findings = privacy_findings(record)
        report.findings.extend(file_findings)
        report.research_queue.extend(research_gaps(record))
        report.recipient_routes[record.relative_path] = route_candidate(
            classification, file_findings, routes,
        )
        report.proposals.append(ActionProposal(
            action="place_in_module",
            source_path=record.relative_path,
            target=classification.module,
            reason=f"Classification source={classification.source}, confidence={classification.confidence:.2f}",
            authority_required="filesystem.write",
        ))
        if record.relative_path in duplicate_paths:
            report.proposals.append(ActionProposal(
                action="review_duplicate",
                source_path=record.relative_path,
                target=None,
                reason="Exact SHA-256 duplicate; deletion is never automatic",
                authority_required="filesystem.delete",
            ))
    return report
