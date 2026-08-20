from __future__ import annotations

import json
import hashlib
import os
import shlex
import subprocess
from pathlib import Path


class AdapterError(RuntimeError):
    pass


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
        receipt.get("authorized") is not True
        or receipt.get("signature_verified") is not True
        or not receipt.get("grant_id")
        or receipt.get("proposal_sha256") != proposal_digest
        or receipt.get("operation") != proposal.get("operation")
        or receipt.get("target") != proposal.get("target")
    ):
        raise AdapterError("Sovereign authorizer did not return a bounded grant")
    return receipt
