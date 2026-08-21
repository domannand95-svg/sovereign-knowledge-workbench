import hashlib
import json
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from sovereign_workbench.adapters import AdapterError, _receipt_payload, request_sovereign_authorization


def _signed_receipt(proposal, key, *, target=None):
    encoded = json.dumps(proposal, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    receipt = {"contract_version":"sovereign.authorization.receipt.v2", "authorized":True,
        "grant_id":"grant-1", "proposal_sha256":hashlib.sha256(encoded).hexdigest(),
        "operation":proposal["operation"], "target":target or proposal["target"], "policy_id":"test-policy",
        "verifying_key":key.public_key().public_bytes_raw().hex()}
    receipt["signature"] = key.sign(_receipt_payload(receipt)).hex()
    return receipt


def _configure(monkeypatch, tmp_path, receipt, key):
    monkeypatch.setenv("SKW_SOVEREIGN_AUTHORIZER", "authorizer")
    monkeypatch.setenv("SKW_GRANT_LEDGER", str(tmp_path / "grants.db"))
    monkeypatch.setenv("SKW_SOVEREIGN_VERIFYING_KEY", key.public_key().public_bytes_raw().hex())
    monkeypatch.setattr("sovereign_workbench.adapters.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=json.dumps(receipt).encode()))


def test_sovereign_receipt_is_independently_verified(monkeypatch, tmp_path):
    proposal = {"operation":"write_review_package", "target":"C:/review.json"}
    key = Ed25519PrivateKey.generate(); receipt = _signed_receipt(proposal, key)
    _configure(monkeypatch, tmp_path, receipt, key)
    assert request_sovereign_authorization(proposal)["grant_id"] == "grant-1"


def test_sovereign_receipt_rejects_target_substitution(monkeypatch, tmp_path):
    proposal = {"operation":"write_review_package", "target":"C:/review.json"}
    key = Ed25519PrivateKey.generate(); receipt = _signed_receipt(proposal, key, target="C:/other.json")
    _configure(monkeypatch, tmp_path, receipt, key)
    with pytest.raises(AdapterError): request_sovereign_authorization(proposal)


def test_sovereign_receipt_rejects_untrusted_signer(monkeypatch, tmp_path):
    proposal = {"operation":"write_review_package", "target":"C:/review.json"}
    key = Ed25519PrivateKey.generate(); receipt = _signed_receipt(proposal, Ed25519PrivateKey.generate())
    _configure(monkeypatch, tmp_path, receipt, key)
    with pytest.raises(AdapterError, match="configured Sovereign policy key"): request_sovereign_authorization(proposal)


def test_sovereign_grant_is_consumed_once(monkeypatch, tmp_path):
    proposal = {"operation":"write_review_package", "target":"C:/review.json"}
    key = Ed25519PrivateKey.generate(); receipt = _signed_receipt(proposal, key)
    _configure(monkeypatch, tmp_path, receipt, key)
    request_sovereign_authorization(proposal)
    with pytest.raises(AdapterError, match="already consumed"): request_sovereign_authorization(proposal)
