import hashlib
import json
from types import SimpleNamespace

import pytest

from sovereign_workbench.adapters import AdapterError, validate_with_bki


@pytest.fixture
def bki_result(tmp_path, monkeypatch):
    source, candidate = tmp_path / "source.md", tmp_path / "candidate.md"
    source.write_bytes(b"source fixture")
    candidate.write_bytes(b"candidate fixture")
    result = {
        "contract_version": "bki.validation.v1", "validator_version": "1.0",
        "executed_at_utc": "2026-10-23T00:00:00Z",
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "candidate_sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
        "source_document_id": None, "candidate_document_id": None,
        "outcome": "PASS — COMPLIANT", "error_code": None,
        "classification_log": [], "diff": "",
    }
    response = SimpleNamespace(returncode=0, stdout=b"")

    def run(*args, **kwargs):
        response.stdout = json.dumps(result).encode()
        return response

    monkeypatch.setattr("sovereign_workbench.adapters.subprocess.run", run)
    return source, candidate, result, response


@pytest.mark.parametrize("outcome,code", [
    ("PASS — COMPLIANT", 0), ("PASS — NORMALIZED", 0), ("FAIL — QUARANTINE", 2),
])
def test_bki_accepts_schema_valid_hash_bound_results(bki_result, tmp_path, outcome, code):
    source, candidate, result, response = bki_result
    result["outcome"], response.returncode = outcome, code
    before = source.read_bytes(), candidate.read_bytes()
    assert validate_with_bki(source, candidate, tmp_path) == result
    assert (source.read_bytes(), candidate.read_bytes()) == before


@pytest.mark.parametrize("mutation", [
    "incomplete", "extra-field", "invalid-time", "unknown-outcome", "wrong-version",
])
def test_bki_rejects_schema_invalid_results(bki_result, tmp_path, mutation):
    source, candidate, result, _ = bki_result
    if mutation == "incomplete":
        result.clear()
        result["contract_version"] = "bki.validation.v1"
    elif mutation == "extra-field":
        result["unexpected"] = True
    elif mutation == "invalid-time":
        result["executed_at_utc"] = "not-a-time"
    elif mutation == "unknown-outcome":
        result["outcome"] = "AUTHORIZED"
    else:
        result["contract_version"] = "bki.validation.v2"
    with pytest.raises(AdapterError, match="schema validation"):
        validate_with_bki(source, candidate, tmp_path)


@pytest.mark.parametrize("field", ["source_sha256", "candidate_sha256"])
def test_bki_rejects_schema_valid_wrong_hashes(bki_result, tmp_path, field):
    source, candidate, result, _ = bki_result
    result[field] = "0" * 64
    with pytest.raises(AdapterError, match="hashes"):
        validate_with_bki(source, candidate, tmp_path)


@pytest.mark.parametrize("index", [0, 1], ids=["source", "candidate"])
def test_bki_rejects_input_changed_during_validation(bki_result, tmp_path, monkeypatch, index):
    source, candidate, result, _ = bki_result

    def run(*args, **kwargs):
        (source, candidate)[index].write_bytes(b"changed fixture")
        return SimpleNamespace(returncode=0, stdout=json.dumps(result).encode())

    monkeypatch.setattr("sovereign_workbench.adapters.subprocess.run", run)
    with pytest.raises(AdapterError, match="changed during validation"):
        validate_with_bki(source, candidate, tmp_path)


@pytest.mark.parametrize("payload", [b"{", b"[]", b"null"])
def test_bki_rejects_malformed_or_non_object_output(bki_result, tmp_path, monkeypatch, payload):
    source, candidate, _, _ = bki_result
    monkeypatch.setattr("sovereign_workbench.adapters.subprocess.run",
                        lambda *a, **k: SimpleNamespace(returncode=0, stdout=payload))
    with pytest.raises(AdapterError):
        validate_with_bki(source, candidate, tmp_path)
