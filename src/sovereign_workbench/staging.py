from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
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
    if set(plan) != required or plan["contract_version"] != "sovereign.workbench.stage-plan.v1" \
            or plan["operation"] != "stage_copy" or plan["authority"] != "none":
        raise StagingError("Invalid staging plan contract")
    core = {key:plan[key] for key in required - {"plan_id","staged_path"}}
    expected = hashlib.sha256(b"SOVEREIGN_WORKBENCH_STAGE_PLAN_V1" + _canonical(core)).hexdigest()
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
    database.execute("PRAGMA journal_mode=WAL")
    return database


def execute(database: sqlite3.Connection, plan: dict, receipt: dict,
            *, max_bytes: int = 100 * 1024 * 1024) -> dict:
    verify_plan(plan)
    if receipt.get("operation") != "stage_copy" or receipt.get("target") != plan["staged_path"] \
            or receipt.get("proposal_sha256") is None:
        raise StagingError("Authorization receipt does not bind the staging effect")
    encoded = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    existing = database.execute("SELECT plan_json,status FROM staged_operations WHERE plan_id=?",
                                (plan["plan_id"],)).fetchone()
    if existing and existing[0] != encoded:
        raise StagingError("Plan identity was reused with altered content")
    if existing and existing[1] == "staged":
        raise StagingError("Plan was already staged")
    database.execute("INSERT OR IGNORE INTO staged_operations(plan_id,plan_json,status) VALUES(?,?,?)",
                     (plan["plan_id"], encoded, "planned"))
    database.execute("UPDATE staged_operations SET status='staging',receipt_json=?,error=NULL WHERE plan_id=?",
                     (json.dumps(receipt, sort_keys=True), plan["plan_id"]))
    database.commit()
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
        partial.replace(target)
        rollback = {"contract_version":"sovereign.workbench.rollback-manifest.v1",
                    "operation":"rollback_stage_copy", "plan_id":plan["plan_id"],
                    "staged_path":str(target), "staged_sha256":staged_hash,
                    "source_path":str(source), "source_sha256":actual_hash, "authority":"none"}
        database.execute("UPDATE staged_operations SET status='staged',rollback_json=? WHERE plan_id=?",
                         (json.dumps(rollback, sort_keys=True), plan["plan_id"]))
        database.commit()
        return rollback
    except (OSError, StagingError) as exc:
        database.execute("UPDATE staged_operations SET status='failed',error=? WHERE plan_id=?",
                         (str(exc)[:1000], plan["plan_id"])); database.commit()
        raise StagingError(str(exc)) from exc


def recover(database: sqlite3.Connection) -> int:
    rows = database.execute("SELECT plan_id,plan_json,status FROM staged_operations WHERE status IN ('staging','rollback_running')").fetchall()
    for plan_id, plan_json, interrupted_status in rows:
        plan = json.loads(plan_json); target = Path(plan["staged_path"])
        status, error = "failed", "interrupted operation requires explicit review and reauthorization"
        if interrupted_status == "staging" and target.is_file() and not target.is_symlink():
            digest, size = _hash_file(target, int(plan["source_size"]))
            if digest == plan["source_sha256"] and size == plan["source_size"]:
                status, error = "staged", None
        elif interrupted_status == "rollback_running" and not target.exists():
            source = Path(plan["source_path"])
            digest, size = _hash_file(source, int(plan["source_size"]))
            if digest == plan["source_sha256"] and size == plan["source_size"]:
                status, error = "rolled_back", None
        database.execute("UPDATE staged_operations SET status=?,error=? WHERE plan_id=?", (status,error,plan_id))
    database.commit(); return len(rows)


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
