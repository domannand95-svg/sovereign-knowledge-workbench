from pathlib import Path

from sovereign_workbench.roles import evaluate_tool_eligibility, load_role_policy


POLICY = Path(__file__).parents[1] / "config" / "roles.v1.json"


def test_researcher_can_request_claim_tool_not_privacy_tool():
    policy = load_role_policy(POLICY)
    assert evaluate_tool_eligibility(policy, "researcher", "research.claims")["eligible"]
    assert not evaluate_tool_eligibility(policy, "researcher", "privacy.detect")["eligible"]


def test_unknown_role_fails_closed():
    result = evaluate_tool_eligibility(load_role_policy(POLICY), "administrator", "archive.inspect")
    assert result == {"eligible": False, "reason": "unknown_role", "human_approval": True}
