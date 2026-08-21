from __future__ import annotations

import json
import hashlib
import os
import shlex
import subprocess
import sqlite3
from pathlib import Path
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


class AdapterError(RuntimeError):
    pass


def _receipt_payload(receipt: dict) -> bytes:
    value = bytearray(b"SOVEREIGN_AUTHORIZATION_RECEIPT_V2")
    for name in ("grant_id", "proposal_sha256", "operation", "target", "policy_id"):
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
    proposal_bytes = json.dumps(
        proposal, ensure_ascii=False, sort_keys=True, separators=(",", ":")
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
        receipt.get("contract_version") != "sovereign.authorization.receipt.v2"
        or receipt.get("authorized") is not True
        or not receipt.get("grant_id")
        or receipt.get("proposal_sha256") != proposal_digest
        or receipt.get("operation") != proposal.get("operation")
        or receipt.get("target") != proposal.get("target")
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
    ledger_path = os.environ.get("SKW_GRANT_LEDGER")
    if not ledger_path:
        raise AdapterError("SKW_GRANT_LEDGER is required for atomic one-time grant consumption")
    with sqlite3.connect(ledger_path) as ledger:
        ledger.execute("CREATE TABLE IF NOT EXISTS consumed_grants(grant_id TEXT PRIMARY KEY, consumed_at TEXT DEFAULT CURRENT_TIMESTAMP)")
        try:
            ledger.execute("INSERT INTO consumed_grants(grant_id) VALUES(?)", (receipt["grant_id"],))
            ledger.commit()
        except sqlite3.IntegrityError as exc:
            raise AdapterError("Sovereign grant was already consumed") from exc
    return receipt
