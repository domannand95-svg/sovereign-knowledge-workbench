"""Plain-language renderings of archive status and search results for people at a terminal.

The JSON outputs stay the machine-readable contract; these functions only present them.
"""
from __future__ import annotations

from .search import STATUS_MESSAGES

ROUTE_NAMES = {
    "MANUAL_REVIEW_REQUIRED": "Needs your review",
    "SPOT_CHECK_ELIGIBLE": "Quality spot check",
    "RECOVERY_REQUIRED": "Text could not be read",
}


def _mb(size: object) -> str:
    return f"{int(size) / (1024 * 1024):.1f} MB"


def render_status(value: dict) -> str:
    counts = value["status"]
    lines = ["Review status",
             f"  Batched (waiting for a decision): {counts['batched']}",
             f"  Reviewed: {counts['reviewed']}",
             f"  Quarantined: {counts['quarantined']}",
             f"  Waiting to be batched: {counts['pending']}",
             f"  Batches created: {counts['batches']}"]
    routing = value.get("routing") or {}
    if any(routing.values()):
        lines.append("  Review routes: " + "; ".join(
            f"{ROUTE_NAMES.get(route, route)} {count}" for route, count in sorted(routing.items()) if count))

    pending = value.get("pending") or {"count": 0, "files": []}
    if pending["count"]:
        lines += ["", f"Waiting to be batched ({pending['count']}):"]
        for item in pending["files"]:
            lines.append(f"  {item['relative_path']}")
            lines.append(f"      {item['explanation']}")
        if pending.get("truncated"):
            lines.append(f"  … and {pending['count'] - len(pending['files'])} more (see --format json)")

    excluded = value.get("excluded") or {"count": 0, "files": []}
    if excluded["count"]:
        lines += ["", f"Not imported because they are too large ({excluded['count']}):"]
        for item in excluded["files"]:
            lines.append(f"  {item['relative_path']} ({_mb(item['size_bytes'])}; limit {_mb(item['limit_bytes'])})")

    stored = value.get("stored_document_text")
    if stored is not None:
        lines.append("")
        if stored["contains_document_text"]:
            rows = ", ".join(f"{name.replace('_', ' ')} {count}" for name, count in stored["rows"].items() if count)
            lines.append(f"Stored document text: YES, in this database ({rows}). See the README "
                         "section 'What the review database stores'.")
        else:
            lines.append("Stored document text: none")
    return "\n".join(lines) + "\n"


def render_search(value: dict) -> str:
    results = value["results"]
    lines = [f"Search \"{value['query']}\": {value['matching_sources']} matching "
             f"file{'' if value['matching_sources'] == 1 else 's'}"
             + (f" (showing {len(results)}; use --limit for more)" if value.get("truncated") else "")]
    for hit in results:
        decision = (hit.get("latest_decision") or {}).get("decision")
        lines.append("")
        lines.append(f"  {hit['relative_path']}  [{STATUS_MESSAGES.get(hit['verification'], hit['verification'])}]"
                     + (f"  decision: {decision}" if decision else "  no decision yet"))
        for match in hit["content_matches"]:
            lines.append(f"      line {match['line']}: {match['text']}")
        if hit.get("content_matches_capped"):
            lines.append("      … more matching lines in this file")
        if hit["metadata_match_labels"]:
            lines.append("      also matched: " + ", ".join(hit["metadata_match_labels"]))
        if hit.get("identical_at_import"):
            lines.append("      identical copy at import: "
                         + ", ".join(other["relative_path"] for other in hit["identical_at_import"]))

    unverified = value.get("unverified_sources") or []
    if unverified:
        lines += ["", f"Not searched because they changed or are missing since import ({len(unverified)}):"]
        for item in unverified:
            line = f"  {item['relative_path']}: {STATUS_MESSAGES.get(item['status'], item['status'])}"
            if item.get("same_content_at"):
                line += ("; the same content is now at " + ", ".join(item["same_content_at"])
                         + " (possibly renamed or moved; kept as a separate record)")
            lines.append(line)
    textless = value.get("not_searchable_sources") or []
    if textless:
        lines += ["", f"Unchanged but with no readable text ({len(textless)}):"]
        lines += [f"  {item['relative_path']}" for item in textless]
    return "\n".join(lines) + "\n"
