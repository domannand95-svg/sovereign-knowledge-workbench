from __future__ import annotations

import json
from pathlib import Path


class RolePolicyError(ValueError):
    pass


def load_role_policy(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("contract_version") != "sovereign.workbench.roles.v1" or not isinstance(value.get("roles"), dict):
        raise RolePolicyError("Invalid role policy contract")
    return value


def evaluate_tool_eligibility(policy: dict, role: str, plugin_id: str) -> dict:
    rules = policy["roles"].get(role)
    if rules is None:
        return {"eligible": False, "reason": "unknown_role", "human_approval": True}
    allowed = plugin_id in rules.get("plugins", [])
    return {
        "eligible": allowed,
        "reason": "role_eligible" if allowed else "plugin_not_allowed_for_role",
        "human_approval": bool(rules.get("human_approval", True)),
    }
