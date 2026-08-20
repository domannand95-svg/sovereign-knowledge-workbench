import hashlib
import json
from types import SimpleNamespace

import pytest

from sovereign_workbench.adapters import AdapterError, request_sovereign_authorization


def test_sovereign_receipt_must_bind_exact_proposal(monkeypatch, tmp_path):
    proposal = {"operation": "write_review_package", "target": "C:/review.json"}
    encoded = json.dumps(proposal, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    receipt = {
        "authorized": True,
        "signature_verified": True,
        "grant_id": "grant-1",
        "proposal_sha256": hashlib.sha256(encoded).hexdigest(),
        "operation": proposal["operation"],
        "target": proposal["target"],
    }
    monkeypatch.setenv("SKW_SOVEREIGN_AUTHORIZER", "authorizer")
    monkeypatch.setenv("SKW_GRANT_LEDGER", str(tmp_path / "grants.db"))
    monkeypatch.setattr(
        "sovereign_workbench.adapters.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=json.dumps(receipt).encode()),
    )
    assert request_sovereign_authorization(proposal)["grant_id"] == "grant-1"


def test_sovereign_receipt_rejects_target_substitution(monkeypatch, tmp_path):
    proposal = {"operation": "write_review_package", "target": "C:/review.json"}
    monkeypatch.setenv("SKW_SOVEREIGN_AUTHORIZER", "authorizer")
    monkeypatch.setenv("SKW_GRANT_LEDGER", str(tmp_path / "grants.db"))
    monkeypatch.setattr(
        "sovereign_workbench.adapters.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=json.dumps({
            "authorized": True,
            "signature_verified": True,
            "grant_id": "grant-1",
            "proposal_sha256": "0" * 64,
            "operation": proposal["operation"],
            "target": "C:/other.json",
        }).encode()),
    )
    with pytest.raises(AdapterError):
        request_sovereign_authorization(proposal)


def test_sovereign_grant_is_consumed_once(monkeypatch, tmp_path):
    proposal = {"operation": "write_review_package", "target": "C:/review.json"}
    encoded = json.dumps(proposal, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    receipt = {"authorized":True,"signature_verified":True,"grant_id":"once","proposal_sha256":hashlib.sha256(encoded).hexdigest(),"operation":proposal["operation"],"target":proposal["target"]}
    monkeypatch.setenv("SKW_SOVEREIGN_AUTHORIZER", "authorizer")
    monkeypatch.setenv("SKW_GRANT_LEDGER", str(tmp_path / "grants.db"))
    monkeypatch.setattr("sovereign_workbench.adapters.subprocess.run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=json.dumps(receipt).encode()))
    request_sovereign_authorization(proposal)
    with pytest.raises(AdapterError, match="already consumed"):
        request_sovereign_authorization(proposal)
