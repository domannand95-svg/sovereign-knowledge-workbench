import hashlib
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from sovereign_workbench.adapters import AdapterError, _receipt_payload, request_sovereign_authorization


REQUESTER = "requester-identity-v1"
NONCE = "ab" * 32


def _proposal():
    return {"operation":"write_review_package", "target":"C:/review.json",
            "requester_identity":REQUESTER, "authorization_nonce":NONCE}


def _signed_receipt(proposal, key, *, target=None, expires_offset=300):
    encoded = json.dumps(proposal, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    issued = datetime.now(timezone.utc) - timedelta(seconds=1)
    receipt = {"contract_version":"sovereign.authorization.receipt.v3", "authorized":True,
        "grant_id":"1" * 64, "proposal_sha256":hashlib.sha256(encoded).hexdigest(),
        "operation":proposal["operation"], "target":target or proposal["target"], "policy_id":"test-policy",
        "requester_identity":proposal["requester_identity"], "capability_request_id":"2" * 64,
        "authorization_nonce":proposal["authorization_nonce"], "issued_at":issued.isoformat(),
        "expires_at":(issued + timedelta(seconds=expires_offset)).isoformat(),
        "verifying_key":key.public_key().public_bytes_raw().hex()}
    receipt["signature"] = key.sign(_receipt_payload(receipt)).hex()
    return receipt


def _configure(monkeypatch, tmp_path, receipt, key):
    monkeypatch.setenv("SKW_SOVEREIGN_AUTHORIZER", "authorizer")
    monkeypatch.setenv("SKW_GRANT_LEDGER", str(tmp_path / "grants.db"))
    monkeypatch.setenv("SKW_SOVEREIGN_VERIFYING_KEY", key.public_key().public_bytes_raw().hex())
    monkeypatch.setenv("SKW_REQUESTER_ID", REQUESTER)
    monkeypatch.setattr("sovereign_workbench.adapters.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=json.dumps(receipt).encode()))


def test_sovereign_receipt_is_independently_verified(monkeypatch, tmp_path):
    proposal = _proposal(); key = Ed25519PrivateKey.generate(); receipt = _signed_receipt(proposal, key)
    _configure(monkeypatch, tmp_path, receipt, key)
    assert request_sovereign_authorization(proposal)["grant_id"] == "1" * 64


def test_sovereign_receipt_rejects_target_substitution(monkeypatch, tmp_path):
    proposal = _proposal(); key = Ed25519PrivateKey.generate()
    receipt = _signed_receipt(proposal, key, target="C:/other.json")
    _configure(monkeypatch, tmp_path, receipt, key)
    with pytest.raises(AdapterError): request_sovereign_authorization(proposal)


def test_sovereign_receipt_rejects_untrusted_signer(monkeypatch, tmp_path):
    proposal = _proposal(); key = Ed25519PrivateKey.generate()
    receipt = _signed_receipt(proposal, Ed25519PrivateKey.generate())
    _configure(monkeypatch, tmp_path, receipt, key)
    with pytest.raises(AdapterError, match="configured Sovereign policy key"): request_sovereign_authorization(proposal)


def test_sovereign_receipt_rejects_expired_lifetime(monkeypatch, tmp_path):
    proposal = _proposal(); key = Ed25519PrivateKey.generate(); receipt = _signed_receipt(proposal, key, expires_offset=0)
    _configure(monkeypatch, tmp_path, receipt, key)
    with pytest.raises(AdapterError, match="expired"): request_sovereign_authorization(proposal)


def test_sovereign_grant_and_nonce_are_consumed_once(monkeypatch, tmp_path):
    proposal = _proposal(); key = Ed25519PrivateKey.generate(); receipt = _signed_receipt(proposal, key)
    _configure(monkeypatch, tmp_path, receipt, key)
    request_sovereign_authorization(proposal)
    with pytest.raises(AdapterError, match="already consumed"): request_sovereign_authorization(proposal)
