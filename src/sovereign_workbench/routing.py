from __future__ import annotations

import json
from pathlib import Path

from .model import Classification, Finding


class RoutingError(ValueError):
    pass


def load_routes(path: Path | None) -> dict[str, dict]:
    if path is None:
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RoutingError("Routing configuration must be a JSON object")
    return value


def route_candidate(
    classification: Classification,
    findings: list[Finding],
    routes: dict[str, dict],
) -> list[str]:
    if findings:
        return ["privacy-review"]
    destinations = []
    for route_id, rule in sorted(routes.items()):
        if classification.module in rule.get("modules", []):
            destinations.append(route_id)
    return destinations or ["manual-review"]
