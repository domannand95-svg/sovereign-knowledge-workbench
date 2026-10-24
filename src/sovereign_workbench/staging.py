from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import secrets
from datetime import datetime, timezone
from pathlib import Path


class StagingError(RuntimeError):
    pass


SCHEMA = """
CREATE TABLE IF NOT EXISTS staged_operations (
  plan_id TEXT PRIMARY KEY,
  plan_json TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('planned','staging','staged','rollback_running','rolled_back','failed')),
  receipt_json TEXT,
  rollback_receipt_json TEXT,
  rollback_json TEXT,
  error TEXT
);
"""


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _hash_file(path: Path, maximum: int) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            if size > maximum:
                raise StagingError("Source exceeds configured staging limit")
            digest.update(chunk)
    return digest.hexdigest(), size


def build_plan(source: Path, staging_root: Path, *, max_bytes: int = 100 * 1024 * 1024) -> dict:
    source = source.resolve(strict=True)
    root = staging_root.resolve(strict=True)
    if source.is_symlink() or not source.is_file() or not root.is_dir():
        raise StagingError("Source and staging root must be regular contained objects")
    source_hash, size = _hash_file(source, max_bytes)
    core = {"contract_version":"sovereign.workbench.stage-plan.v1", "operation":"stage_copy",
            "source_path":str(source), "source_sha256":source_hash, "source_size":size,
            "staging_root":str(root), "authority":"none"}
    plan_id = hashlib.sha256(b"SOVEREIGN_WORKBENCH_STAGE_PLAN_V1" + _canonical(core)).hexdigest()
    core["plan_id"] = plan_id
    core["staged_path"] = str(root / plan_id / source.name)
    return core


def verify_plan(plan: dict) -> None:
    required = {"contract_version","operation","source_path","source_sha256","source_size",
                "staging_root","authority","plan_id","staged_path"}
    version = plan.get("contract_version") if isinstance(plan, dict) else None
    retry = version == "sovereign.workbench.stage-plan.v2"
    if retry:
        required |= {"retry_of_plan_id", "parent_snapshot_sha256", "attempt_nonce"}
        for field in ("retry_of_plan_id", "parent_snapshot_sha256", "attempt_nonce"):
            value = plan.get(field)
            if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                raise StagingError("Invalid retry attempt identity")
    if not isinstance(plan, dict) or set(plan) != required or version not in {
            "sovereign.workbench.stage-plan.v1", "sovereign.workbench.stage-plan.v2"} \
            or plan["operation"] != "stage_copy" or plan["authority"] != "none":
        raise StagingError("Invalid staging plan contract")
    core = {key:plan[key] for key in required - {"plan_id","staged_path"}}
    domain = b"SOVEREIGN_WORKBENCH_STAGE_PLAN_V2" if retry else b"SOVEREIGN_WORKBENCH_STAGE_PLAN_V1"
    expected = hashlib.sha256(domain + _canonical(core)).hexdigest()
    root = Path(plan["staging_root"]).resolve(strict=True)
    target = Path(plan["staged_path"])
    if plan["plan_id"] != expected or target != root / expected / Path(plan["source_path"]).name:
        raise StagingError("Staging plan identity or containment mismatch")


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    database = sqlite3.connect(path)
    database.executescript(SCHEMA)
    columns = {row[1] for row in database.execute("PRAGMA table_info(staged_operations)")}
    if "rollback_receipt_json" not in columns:
        database.execute("ALTER TABLE staged_operations ADD COLUMN rollback_receipt_json TEXT")
    if "publication_state" not in columns:
        # NULL means legacy/unknown, never proof that publication was not attempted.
        database.execute("ALTER TABLE staged_operations ADD COLUMN publication_state TEXT "
                         "CHECK(publication_state IN ('not_attempted','publication_pending','published'))")
    database.executescript("""
    CREATE TABLE IF NOT EXISTS staging_retry_links (
      retry_plan_id TEXT PRIMARY KEY,
      parent_plan_id TEXT NOT NULL UNIQUE,
      parent_snapshot_json TEXT NOT NULL,
      parent_snapshot_sha256 TEXT NOT NULL,
      reason TEXT NOT NULL,
      created_at TEXT NOT NULL,
      authority TEXT NOT NULL CHECK(authority='none')
    );
    CREATE TRIGGER IF NOT EXISTS staging_retry_links_no_update
      BEFORE UPDATE ON staging_retry_links BEGIN SELECT RAISE(ABORT, 'retry links are immutable'); END;
    CREATE TRIGGER IF NOT EXISTS staging_retry_links_no_delete
      BEFORE DELETE ON staging_retry_links BEGIN SELECT RAISE(ABORT, 'retry links are immutable'); END;
    """)
    database.execute("PRAGMA journal_mode=WAL")
    database.execute("PRAGMA synchronous=FULL")
    return database


def _attempt_snapshot(database: sqlite3.Connection, plan_id: str) -> dict:
    cursor = database.execute("SELECT * FROM staged_operations WHERE plan_id=?", (plan_id,))
    row = cursor.fetchone()
    if row is None:
        raise StagingError("Unknown parent staging attempt")
    return dict(zip((column[0] for column in cursor.description), row))


def prepare_retry(database: sqlite3.Connection, parent_plan_id: str, *, reason: str,
                  max_bytes: int = 100 * 1024 * 1024) -> dict:
    """Record a linked proposal, never permission to repeat a prior effect.

    Uncertain and legacy attempts may have a linked proposal recorded for review,
    but remain ineligible for execution. No authorizer or filesystem writer runs.
    """
    if database.in_transaction:
        raise StagingError("Retry preparation requires a committed connection")
    if not isinstance(reason, str) or not reason.strip() or len(reason.encode()) > 1000:
        raise StagingError("Retry requires a bounded nonempty review reason")
    database.execute("BEGIN IMMEDIATE")
    try:
        if database.execute("SELECT 1 FROM staging_retry_links WHERE parent_plan_id=?", (parent_plan_id,)).fetchone():
            raise StagingError("Parent already has a linked retry; inspect that attempt instead")
        parent = _attempt_snapshot(database, parent_plan_id)
        if parent["status"] != "failed":
            raise StagingError("Only an inspected failed attempt may receive a retry proposal")
        parent_plan = json.loads(parent["plan_json"])
        verify_plan(parent_plan)
        if parent_plan["plan_id"] != parent_plan_id:
            raise StagingError("Parent plan identity mismatch")
        snapshot_json = _canonical(parent).decode()
        snapshot_sha = hashlib.sha256(snapshot_json.encode()).hexdigest()
        plan = build_plan(Path(parent_plan["source_path"]), Path(parent_plan["staging_root"]), max_bytes=max_bytes)
        plan.update(contract_version="sovereign.workbench.stage-plan.v2",
                    retry_of_plan_id=parent_plan_id, parent_snapshot_sha256=snapshot_sha,
                    attempt_nonce=secrets.token_hex(32))
        core = {key: value for key, value in plan.items() if key not in {"plan_id", "staged_path"}}
        plan_id = hashlib.sha256(b"SOVEREIGN_WORKBENCH_STAGE_PLAN_V2" + _canonical(core)).hexdigest()
        plan["plan_id"] = plan_id
        plan["staged_path"] = str(Path(plan["staging_root"]) / plan_id / Path(plan["source_path"]).name)
        verify_plan(plan)
        database.execute("INSERT INTO staged_operations(plan_id,plan_json,status) VALUES(?,?,'planned')",
                         (plan_id, _canonical(plan).decode()))
        database.execute("INSERT INTO staging_retry_links VALUES(?,?,?,?,?,?,?)",
                         (plan_id, parent_plan_id, snapshot_json, snapshot_sha, reason.strip(),
                          datetime.now(timezone.utc).isoformat(), "none"))
        database.commit()
        return plan
    except BaseException:
        database.rollback()
        raise


def validate_execution_preconditions(database: sqlite3.Connection, plan: dict) -> None:
    """Preflight before fresh authorization; execute rechecks before claiming.

    Local phase evidence does not prove absence of an arbitrary external effect.
    Legacy or possibly published attempts require a separate repair contract.
    """
    verify_plan(plan)
    existing = database.execute("SELECT plan_json,status,receipt_json,rollback_json,rollback_receipt_json "
                                "FROM staged_operations WHERE plan_id=?", (plan["plan_id"],)).fetchone()
    if existing and existing[0] != _canonical(plan).decode():
        raise StagingError("Plan identity was reused with altered content")
    if existing and (existing[1] != "planned" or any(v is not None for v in existing[2:])):
        raise StagingError("Prior staging attempt requires explicit review; evidence was preserved")
    if plan["contract_version"] != "sovereign.workbench.stage-plan.v2":
        return
    link = database.execute("SELECT parent_plan_id,parent_snapshot_json,parent_snapshot_sha256 "
                            "FROM staging_retry_links WHERE retry_plan_id=?", (plan["plan_id"],)).fetchone()
    if not link or link[0] != plan["retry_of_plan_id"] or link[2] != plan["parent_snapshot_sha256"]:
        raise StagingError("Retry lacks an intact recorded parent link")
    if hashlib.sha256(link[1].encode()).hexdigest() != link[2]:
        raise StagingError("Retry parent provenance hash mismatch")
    parent = _attempt_snapshot(database, link[0])
    if _canonical(parent).decode() != link[1]:
        raise StagingError("Parent attempt changed since retry proposal; review required")
    if parent["status"] != "failed" or parent["publication_state"] != "not_attempted" or parent["rollback_json"] is not None:
        raise StagingError("Prior effect is uncertain or published; linked retry is not authorization to repeat it")
    parent_plan = json.loads(parent["plan_json"])
    verify_plan(parent_plan)
    target = Path(parent_plan["staged_path"])
    if target.exists() or target.is_symlink():
        raise StagingError("Prior destination exists; retry requires explicit reconciliation")
    # The effect, source/root and lineage are fixed by the new plan hash, which
    # the existing caller binds into a freshly signed and consumed proposal.
    if (plan["source_path"], plan["staging_root"]) != (parent_plan["source_path"], parent_plan["staging_root"]):
        raise StagingError("Retry changed source/root outside its recorded lineage")


def execute(database: sqlite3.Connection, plan: dict, receipt: dict,
            *, max_bytes: int = 100 * 1024 * 1024) -> dict:
    validate_execution_preconditions(database, plan)
    if receipt.get("operation") != "stage_copy" or receipt.get("target") != plan["staged_path"] \
            or receipt.get("proposal_sha256") is None:
        raise StagingError("Authorization receipt does not bind the staging effect")
    encoded = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    # A prior attempt is retained for inspected recovery, never overwritten by
    # retry. Receipt verification/consumption remains the caller's boundary.
    receipt_json = json.dumps(receipt, sort_keys=True)
    existing = database.execute(
        "SELECT plan_json,status,receipt_json,rollback_json,rollback_receipt_json "
        "FROM staged_operations WHERE plan_id=?", (plan["plan_id"],)).fetchone()
    if existing and existing[0] != encoded:
        raise StagingError("Plan identity was reused with altered content")
    if existing and (existing[1] != "planned" or any(value is not None for value in existing[2:])):
        raise StagingError("Prior staging attempt requires explicit review; evidence was preserved")
    database.execute("INSERT OR IGNORE INTO staged_operations(plan_id,plan_json,status) VALUES(?,?,?)",
                     (plan["plan_id"], encoded, "planned"))
    claim = database.execute(
        "UPDATE staged_operations SET status='staging',receipt_json=?,publication_state='not_attempted',error=NULL "
        "WHERE plan_id=? AND status='planned' AND receipt_json IS NULL "
        "AND rollback_json IS NULL AND rollback_receipt_json IS NULL",
        (receipt_json, plan["plan_id"]))
    if claim.rowcount != 1:
        database.rollback()
        raise StagingError("Staging attempt claim failed; publication was not attempted")
    database.commit()
    rollback_json = None
    source, target = Path(plan["source_path"]), Path(plan["staged_path"])
    try:
        actual_hash, actual_size = _hash_file(source, max_bytes)
        if actual_hash != plan["source_sha256"] or actual_size != plan["source_size"]:
            raise StagingError("Source changed after staging plan creation")
        target.parent.mkdir(parents=True, exist_ok=False)
        partial = target.with_suffix(target.suffix + ".partial")
        with source.open("rb") as reader, partial.open("xb") as writer:
            shutil.copyfileobj(reader, writer, 1024 * 1024)
            writer.flush(); os.fsync(writer.fileno())
        staged_hash, staged_size = _hash_file(partial, max_bytes)
        if (staged_hash, staged_size) != (actual_hash, actual_size):
            raise StagingError("Staged copy verification failed")
        rollback = {"contract_version":"sovereign.workbench.rollback-manifest.v1",
                    "operation":"rollback_stage_copy", "plan_id":plan["plan_id"],
                    "staged_path":str(target), "staged_sha256":staged_hash,
                    "source_path":str(source), "source_sha256":actual_hash, "authority":"none"}
        # Durable rollback intent precedes publication. Intent alone is not a
        # completed effect, and never authorizes recovery to publish a partial.
        rollback_json = json.dumps(rollback, sort_keys=True)
        intent = database.execute(
            "UPDATE staged_operations SET rollback_json=?,publication_state='publication_pending' WHERE plan_id=? AND status='staging' "
            "AND receipt_json=? AND rollback_json IS NULL",
            (rollback_json, plan["plan_id"], receipt_json))
        if intent.rowcount != 1:
            database.rollback()
            raise StagingError("Durable rollback intent transition failed; publication was not attempted")
        # Do not publish unless this commit returns successfully. A database
        # exception leaves the destination unpublished for inspected recovery.
        database.commit()
        partial.replace(target)
        completion = database.execute(
            "UPDATE staged_operations SET status='staged',publication_state='published' WHERE plan_id=? AND status='staging' "
            "AND receipt_json=? AND rollback_json=?",
            (plan["plan_id"], receipt_json, rollback_json))
        if completion.rowcount != 1:
            database.rollback()
            raise StagingError("Staging state changed after publication; explicit review required")
        database.commit()
        return rollback
    except (OSError, StagingError) as exc:
        # Preserve intent and avoid clobbering an independently changed attempt.
        database.execute(
            "UPDATE staged_operations SET status='failed',error=? WHERE plan_id=? "
            "AND status='staging' AND receipt_json=? AND rollback_json IS ?",
            (str(exc)[:1000], plan["plan_id"], receipt_json, rollback_json))
        database.commit()
        raise StagingError(str(exc)) from exc



def recover(database: sqlite3.Connection) -> int:
    """Observe quiesced attempts; stored receipt shape is not signature verification.

    Callers must preserve verified receipt/ledger provenance. This function does
    not consume a grant or authorize publication, deletion, or retry.
    """
    rows = database.execute("SELECT plan_id,plan_json,status,receipt_json,rollback_json,rollback_receipt_json FROM staged_operations WHERE status IN ('staging','rollback_running')").fetchall()
    for plan_id, plan_json, interrupted_status, receipt_json, rollback_json, rollback_receipt_json in rows:
        status, error = "failed", "interrupted operation requires explicit review and reauthorization"
        try:
            plan = json.loads(plan_json)
            verify_plan(plan)
            if plan_id != plan["plan_id"]:
                raise StagingError("Stored plan identity mismatch")
            target, source = Path(plan["staged_path"]), Path(plan["source_path"])
            expected = {"contract_version":"sovereign.workbench.rollback-manifest.v1",
                        "operation":"rollback_stage_copy", "plan_id":plan_id,
                        "staged_path":str(target), "staged_sha256":plan["source_sha256"],
                        "source_path":str(source), "source_sha256":plan["source_sha256"], "authority":"none"}
            if not rollback_json or json.loads(rollback_json) != expected:
                raise StagingError("Missing or inconsistent durable rollback intent")
            receipt = json.loads(receipt_json) if receipt_json else {}
            if (not isinstance(receipt, dict) or receipt.get("operation") != "stage_copy" or receipt.get("target") != str(target)
                    or not receipt.get("proposal_sha256")):
                raise StagingError("Missing original bound authorization evidence")
            if source.is_symlink() or not source.is_file():
                raise StagingError("Source is not a regular file")
            wanted = (plan["source_sha256"], int(plan["source_size"]))
            if _hash_file(source, wanted[1]) != wanted:
                raise StagingError("Source changed since original staging")
            if interrupted_status == "staging" and target.is_file() and not target.is_symlink():
                if _hash_file(target, wanted[1]) == wanted:
                    status, error = "staged", None
            elif interrupted_status == "rollback_running" and not target.exists() and not target.is_symlink():
                rollback_receipt = json.loads(rollback_receipt_json) if rollback_receipt_json else {}
                if (not isinstance(rollback_receipt, dict) or rollback_receipt.get("operation") != "rollback_stage_copy"
                        or rollback_receipt.get("target") != str(target)):
                    raise StagingError("Missing separately bound rollback authorization evidence")
                # Observing a completed rollback is not permission to repeat it.
                status, error = "rolled_back", None
        except (OSError, ValueError, TypeError, KeyError, StagingError) as exc:
            error = f"interrupted operation requires explicit review and reauthorization: {exc}"
        database.execute("UPDATE staged_operations SET status=?,error=? WHERE plan_id=? AND status=?",
                         (status,error,plan_id,interrupted_status))
    database.commit()
    return len(rows)



def rollback(database: sqlite3.Connection, plan_id: str, receipt: dict) -> dict:
    row = database.execute("SELECT plan_json,status,rollback_json FROM staged_operations WHERE plan_id=?",
                           (plan_id,)).fetchone()
    if not row or row[1] != "staged" or not row[2]:
        raise StagingError("Only a verified staged operation can be rolled back")
    plan, manifest = json.loads(row[0]), json.loads(row[2])
    if receipt.get("operation") != "rollback_stage_copy" or receipt.get("target") != manifest["staged_path"]:
        raise StagingError("Authorization receipt does not bind the rollback effect")
    database.execute("UPDATE staged_operations SET status='rollback_running',rollback_receipt_json=? WHERE plan_id=?",
                     (json.dumps(receipt, sort_keys=True), plan_id)); database.commit()
    target, source = Path(manifest["staged_path"]), Path(manifest["source_path"])
    try:
        staged_hash, staged_size = _hash_file(target, int(plan["source_size"]))
        source_hash, source_size = _hash_file(source, int(plan["source_size"]))
        if staged_hash != manifest["staged_sha256"] or source_hash != manifest["source_sha256"] \
                or staged_size != source_size:
            raise StagingError("Rollback identity verification failed")
        target.unlink()
        target.parent.rmdir()
        result = dict(manifest); result["status"] = "rolled_back"
        database.execute("UPDATE staged_operations SET status='rolled_back',rollback_json=? WHERE plan_id=?",
                         (json.dumps(result, sort_keys=True), plan_id)); database.commit()
        return result
    except (OSError, StagingError) as exc:
        database.execute("UPDATE staged_operations SET status='failed',error=? WHERE plan_id=?",
                         (str(exc)[:1000], plan_id)); database.commit()
        raise StagingError(str(exc)) from exc


def status_counts(database: sqlite3.Connection) -> dict[str, int]:
    values = {key:0 for key in ("planned","staging","staged","rollback_running","rolled_back","failed")}
    for status, count in database.execute("SELECT status,COUNT(*) FROM staged_operations GROUP BY status"):
        values[status] = count
    return values
