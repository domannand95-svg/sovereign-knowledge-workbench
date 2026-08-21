from __future__ import annotations

import json
import hashlib
import os
import shlex
import subprocess
import sqlite3
import secrets
from datetime import datetime, timezone
from pathlib import Path
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


class AdapterError(RuntimeError):
    pass


def _receipt_payload(receipt: dict) -> bytes:
    value = bytearray(b"SOVEREIGN_AUTHORIZATION_RECEIPT_V3")
    for name in ("grant_id", "proposal_sha256", "operation", "target", "policy_id",
                 "requester_identity", "capability_request_id", "authorization_nonce",
                 "issued_at", "expires_at"):
        field = str(receipt.get(name, "")).encode()
        value.extend(len(field).to_bytes(8, "big"))
        value.extend(field)
    return bytes(value)


def validate_with_bki(source: Path, candidate: Path, bki_root: Path) -> dict:
    command = [
        os.environ.get("SKW_PYTHON", "python"), "-m", "tooling.normalization.cli",
        "--source", str(source.resolve()), "--candidate", str(candidate.resolve()),
        "--format", "bki.validation.v1",
    ]
    completed = subprocess.run(command, cwd=bki_root.resolve(), capture_output=True, timeout=30, check=False)
    if completed.returncode not in (0, 2):
        raise AdapterError(f"BKI failed closed with exit {completed.returncode}")
    try:
        result = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdapterError("BKI returned malformed output") from exc
    if result.get("contract_version") != "bki.validation.v1":
        raise AdapterError("BKI contract identity mismatch")
    return result


def request_sovereign_authorization(proposal: dict) -> dict:
    raw_command = os.environ.get("SKW_SOVEREIGN_AUTHORIZER")
    if not raw_command:
        raise AdapterError("No Sovereign authorizer configured; effect remains proposed")
    requester_identity = os.environ.get("SKW_REQUESTER_ID")
    if not requester_identity:
        raise AdapterError("SKW_REQUESTER_ID is required for capability-bound authorization")
    bounded_proposal = dict(proposal)
    bounded_proposal.setdefault("requester_identity", requester_identity)
    bounded_proposal.setdefault("authorization_nonce", secrets.token_hex(32))
    if bounded_proposal["requester_identity"] != requester_identity:
        raise AdapterError("Proposal requester identity does not match configured identity")
    proposal_bytes = json.dumps(
        bounded_proposal, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    proposal_digest = hashlib.sha256(proposal_bytes).hexdigest()
    completed = subprocess.run(
        shlex.split(raw_command), input=proposal_bytes,
        capture_output=True, timeout=30, check=False,
    )
    if completed.returncode != 0:
        raise AdapterError("Sovereign authorizer denied or failed closed")
    try:
        receipt = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdapterError("Sovereign authorizer returned malformed output") from exc
    if (
        receipt.get("contract_version") != "sovereign.authorization.receipt.v3"
        or receipt.get("authorized") is not True
        or not receipt.get("grant_id")
        or receipt.get("proposal_sha256") != proposal_digest
        or receipt.get("operation") != bounded_proposal.get("operation")
        or receipt.get("target") != bounded_proposal.get("target")
        or receipt.get("requester_identity") != requester_identity
        or receipt.get("authorization_nonce") != bounded_proposal.get("authorization_nonce")
        or len(str(receipt.get("capability_request_id", ""))) != 64
    ):
        raise AdapterError("Sovereign authorizer did not return a bounded grant")
    trusted_key = os.environ.get("SKW_SOVEREIGN_VERIFYING_KEY")
    if not trusted_key:
        raise AdapterError("SKW_SOVEREIGN_VERIFYING_KEY is required for independent receipt verification")
    if receipt.get("verifying_key") != trusted_key:
        raise AdapterError("Receipt was not signed by the configured Sovereign policy key")
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(trusted_key)).verify(
            bytes.fromhex(str(receipt.get("signature", ""))), _receipt_payload(receipt)
        )
    except (ValueError, InvalidSignature) as exc:
        raise AdapterError("Sovereign receipt signature is invalid") from exc
    try:
        issued_at = datetime.fromisoformat(str(receipt["issued_at"]).replace("Z", "+00:00"))
        expires_at = datetime.fromisoformat(str(receipt["expires_at"]).replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        if issued_at.tzinfo is None or expires_at.tzinfo is None or not (issued_at <= now < expires_at):
            raise ValueError("outside lifetime")
        if (expires_at - issued_at).total_seconds() > 900:
            raise ValueError("lifetime too broad")
    except (KeyError, TypeError, ValueError) as exc:
        raise AdapterError("Sovereign receipt is expired or has an invalid lifetime") from exc
    ledger_path = os.environ.get("SKW_GRANT_LEDGER")
    if not ledger_path:
        raise AdapterError("SKW_GRANT_LEDGER is required for atomic one-time grant consumption")
    with sqlite3.connect(ledger_path) as ledger:
        ledger.execute("CREATE TABLE IF NOT EXISTS consumed_grants(grant_id TEXT PRIMARY KEY, authorization_nonce TEXT UNIQUE, receipt_json TEXT NOT NULL DEFAULT '{}', consumed_at TEXT DEFAULT CURRENT_TIMESTAMP)")
        columns = {row[1] for row in ledger.execute("PRAGMA table_info(consumed_grants)")}
        if "authorization_nonce" not in columns:
            ledger.execute("ALTER TABLE consumed_grants ADD COLUMN authorization_nonce TEXT")
            ledger.execute("CREATE UNIQUE INDEX IF NOT EXISTS consumed_grants_nonce_idx ON consumed_grants(authorization_nonce)")
        if "receipt_json" not in columns:
            ledger.execute("ALTER TABLE consumed_grants ADD COLUMN receipt_json TEXT NOT NULL DEFAULT '{}'")
        try:
            ledger.execute("INSERT INTO consumed_grants(grant_id,authorization_nonce,receipt_json) VALUES(?,?,?)",
                (receipt["grant_id"], receipt["authorization_nonce"],
                 json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":"))))
            ledger.commit()
        except sqlite3.IntegrityError as exc:
            raise AdapterError("Sovereign grant or authorization nonce was already consumed") from exc
    return receipt
