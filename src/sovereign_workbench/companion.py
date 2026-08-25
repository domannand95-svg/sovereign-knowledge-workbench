from __future__ import annotations

import argparse
import hmac
import json
import os
import secrets
import subprocess
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import RLock
from typing import Callable
from urllib.parse import parse_qs, urlparse

from .adapters import AdapterError, request_sovereign_authorization
from .jobs import connect as connect_jobs
from .reviews import list_candidates, verify_decisions
from .staging import (StagingError, build_plan, connect as connect_staging,
                      execute, rollback, status_counts)

CONTRACT_VERSION = "sovereign.workbench.companion.v1"


class CompanionError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class CompanionConfig:
    state_db: Path
    staging_db: Path
    token: str
    allowed_origins: frozenset[str] = frozenset()
    plan_ttl_seconds: int = 300
    token_ttl_seconds: int = 8 * 60 * 60
    rate_limit_per_minute: int = 120

    def __post_init__(self) -> None:
        for origin in self.allowed_origins:
            parsed = urlparse(origin)
            if (any(ord(character) < 0x20 or ord(character) == 0x7f for character in origin)
                    or parsed.scheme not in {"http", "https"} or not parsed.hostname
                    or parsed.username or parsed.password or parsed.path or parsed.params
                    or parsed.query or parsed.fragment):
                raise ValueError("Allowed origins must be canonical HTTP(S) origins")


@dataclass
class CompanionService:
    config: CompanionConfig
    authorizer: Callable[[dict], dict] = request_sovereign_authorization
    plans: dict[str, dict] = field(default_factory=dict)
    started_at: float = field(default_factory=time.monotonic)
    lock: RLock = field(default_factory=RLock)

    def token_valid(self) -> bool:
        return time.monotonic() - self.started_at < self.config.token_ttl_seconds

    def _audit(self, event_type: str, *, plan_id: str | None = None,
               detail: dict | None = None) -> None:
        with connect_staging(self.config.staging_db) as database:
            database.execute("""CREATE TABLE IF NOT EXISTS companion_audit (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                occurred_at TEXT NOT NULL,
                event_type TEXT NOT NULL,
                plan_id TEXT,
                detail_json TEXT NOT NULL
            )""")
            database.execute(
                "INSERT INTO companion_audit(occurred_at,event_type,plan_id,detail_json) VALUES(?,?,?,?)",
                (datetime.now(timezone.utc).isoformat(), event_type, plan_id,
                 json.dumps(detail or {}, sort_keys=True, separators=(",", ":"))),
            )
            database.commit()

    def health(self) -> dict:
        with connect_staging(self.config.staging_db) as database:
            staging = status_counts(database)
        return {"status": "ok", "authority": "none", "loopback_only": True,
                "contract_version": CONTRACT_VERSION, "staging": staging}

    def reviews(self, limit: int) -> dict:
        with connect_jobs(self.config.state_db) as database:
            return {"items": list_candidates(database, limit=limit),
                    "integrity": verify_decisions(database)}

    def staging_status(self) -> dict:
        with connect_staging(self.config.staging_db) as database:
            return status_counts(database)

    def plan(self, body: dict) -> dict:
        _exact_keys(body, {"source_path", "staging_root"},
                    optional={"max_bytes"})
        plan = build_plan(Path(body["source_path"]), Path(body["staging_root"]),
                          max_bytes=_bounded_int(body.get("max_bytes", 100 * 1024 * 1024), 1, 1024 ** 3))
        with self.lock:
            self.plans[plan["plan_id"]] = {
                "plan": plan, "created_at": time.monotonic(), "state": "planned",
            }
        self._audit("plan_created", plan_id=plan["plan_id"],
                    detail={"source_sha256": plan["source_sha256"], "authority": "none"})
        return plan

    def _claim_plan(self, plan_id: str, expected: str, claimed: str) -> dict:
        with self.lock:
            held = self.plans.get(plan_id)
            if held is None:
                raise CompanionError(HTTPStatus.NOT_FOUND, "Unknown server-held staging plan")
            if time.monotonic() - held["created_at"] >= self.config.plan_ttl_seconds:
                held["state"] = "expired"
                raise CompanionError(HTTPStatus.GONE, "Staging plan expired; create a new preview")
            if held["state"] != expected:
                raise CompanionError(HTTPStatus.CONFLICT, "Staging plan is not available for this operation")
            held["state"] = claimed
            return dict(held["plan"])

    def _set_plan_state(self, plan_id: str, state: str) -> None:
        with self.lock:
            if plan_id in self.plans:
                self.plans[plan_id]["state"] = state

    def execute(self, plan_id: str, body: dict) -> dict:
        _exact_keys(body, set())
        plan = self._claim_plan(plan_id, "planned", "authorizing")
        proposal = {"operation": "stage_copy", "target": plan["staged_path"],
                    "report_sha256": plan["plan_id"]}
        try:
            receipt = self.authorizer(proposal)
            with connect_staging(self.config.staging_db) as database:
                manifest = execute(database, plan, receipt)
        except Exception:
            self._set_plan_state(plan_id, "failed")
            self._audit("execution_failed", plan_id=plan_id)
            raise
        self._set_plan_state(plan_id, "executed")
        self._audit("execution_completed", plan_id=plan_id,
                    detail={"grant_id": receipt.get("grant_id", "test-authorizer")})
        return {"status": "staged", "manifest": manifest}

    def rollback(self, plan_id: str, body: dict) -> dict:
        _exact_keys(body, set())
        with self.lock:
            held = self.plans.get(plan_id)
            if held and held["state"] == "rolling_back":
                raise CompanionError(HTTPStatus.CONFLICT, "Rollback is already in progress")
            if held:
                held["state"] = "rolling_back"
        try:
            with connect_staging(self.config.staging_db) as database:
                row = database.execute("SELECT rollback_json FROM staged_operations WHERE plan_id=?",
                                       (plan_id,)).fetchone()
                if not row or not row[0]:
                    raise CompanionError(HTTPStatus.NOT_FOUND, "Unknown staged rollback manifest")
                manifest = json.loads(row[0])
                proposal = {"operation": "rollback_stage_copy", "target": manifest["staged_path"],
                            "report_sha256": plan_id}
                receipt = self.authorizer(proposal)
                result = rollback(database, plan_id, receipt)
        except Exception:
            self._set_plan_state(plan_id, "failed")
            self._audit("rollback_failed", plan_id=plan_id)
            raise
        self._set_plan_state(plan_id, "rolled_back")
        self._audit("rollback_completed", plan_id=plan_id,
                    detail={"grant_id": receipt.get("grant_id", "test-authorizer")})
        return {"status": "rolled_back", "manifest": result}


def _exact_keys(body: dict, required: set[str], *, optional: set[str] | None = None) -> None:
    optional = optional or set()
    if not isinstance(body, dict) or set(body) - required - optional or not required.issubset(body):
        raise CompanionError(HTTPStatus.BAD_REQUEST, "Request fields do not match the endpoint contract")


def _bounded_int(value: object, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise CompanionError(HTTPStatus.BAD_REQUEST, "Numeric value is outside the allowed range")
    return value


def make_handler(service: CompanionService):
    request_times: dict[str, deque[float]] = defaultdict(deque)
    rate_lock = RLock()

    class Handler(BaseHTTPRequestHandler):
        server_version = "SovereignWorkbenchCompanion/1"

        def _cors(self) -> bool:
            origin = self.headers.get("Origin")
            if origin and origin not in service.config.allowed_origins:
                self._send(HTTPStatus.FORBIDDEN, {"error": "Origin is not allowed"})
                return False
            return True

        def _authorized(self) -> bool:
            supplied = self.headers.get("Authorization", "")
            expected = f"Bearer {service.config.token}"
            if not service.token_valid():
                self._send(HTTPStatus.UNAUTHORIZED, {"error": "Companion token expired; restart required"})
                return False
            if not hmac.compare_digest(supplied, expected):
                self._send(HTTPStatus.UNAUTHORIZED, {"error": "Valid companion token required"})
                return False
            return True

        def _contract_matches(self) -> bool:
            if self.headers.get("X-Companion-Contract") != CONTRACT_VERSION:
                self._send(HTTPStatus.PRECONDITION_FAILED,
                           {"error": "Companion contract version mismatch"})
                return False
            return True

        def _within_rate_limit(self) -> bool:
            now = time.monotonic()
            client = self.client_address[0]
            with rate_lock:
                recent = request_times[client]
                while recent and now - recent[0] >= 60:
                    recent.popleft()
                if len(recent) >= service.config.rate_limit_per_minute:
                    self._send(HTTPStatus.TOO_MANY_REQUESTS,
                               {"error": "Companion request limit exceeded"})
                    return False
                recent.append(now)
            return True

        def _body(self) -> dict:
            try:
                size = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise CompanionError(HTTPStatus.BAD_REQUEST, "Invalid content length") from exc
            if size > 64 * 1024:
                raise CompanionError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Request body is too large")
            if size == 0:
                return {}
            try:
                value = json.loads(self.rfile.read(size))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise CompanionError(HTTPStatus.BAD_REQUEST, "Malformed JSON") from exc
            if not isinstance(value, dict):
                raise CompanionError(HTTPStatus.BAD_REQUEST, "JSON body must be an object")
            return value

        def _send(self, status: int, payload: dict) -> None:
            data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            origin = self.headers.get("Origin")
            if origin in service.config.allowed_origins:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
            self.end_headers()
            self.wfile.write(data)

        def _dispatch(self) -> tuple[int, dict]:
            parsed = urlparse(self.path)
            parts = [part for part in parsed.path.split("/") if part]
            if self.command == "GET" and parts == ["v1", "health"]:
                return HTTPStatus.OK, service.health()
            if self.command == "GET" and parts == ["v1", "reviews"]:
                raw = parse_qs(parsed.query).get("limit", ["100"])[0]
                return HTTPStatus.OK, service.reviews(_bounded_int(int(raw), 1, 1000))
            if self.command == "GET" and parts == ["v1", "staging", "status"]:
                return HTTPStatus.OK, service.staging_status()
            if self.command == "POST" and parts == ["v1", "staging", "plans"]:
                return HTTPStatus.CREATED, service.plan(self._body())
            if self.command == "POST" and len(parts) == 5 and parts[:3] == ["v1", "staging", "plans"]:
                if parts[4] == "execute":
                    return HTTPStatus.OK, service.execute(parts[3], self._body())
                if parts[4] == "rollback":
                    return HTTPStatus.OK, service.rollback(parts[3], self._body())
            raise CompanionError(HTTPStatus.NOT_FOUND, "Unknown companion endpoint")

        def _handle(self) -> None:
            if (not self._within_rate_limit() or not self._cors()
                    or not self._contract_matches() or not self._authorized()):
                return
            try:
                status, payload = self._dispatch()
            except (CompanionError, AdapterError, StagingError, OSError, ValueError) as exc:
                status = exc.status if isinstance(exc, CompanionError) else HTTPStatus.BAD_REQUEST
                self._send(status, {"error": str(exc)})
                return
            self._send(status, payload)

        do_GET = _handle
        do_POST = _handle

        def do_OPTIONS(self) -> None:
            if not self._cors():
                return
            self.send_response(HTTPStatus.NO_CONTENT)
            self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
            self.send_header("Access-Control-Allow-Headers",
                             "Authorization,Content-Type,X-Companion-Contract")
            self.send_header("Access-Control-Max-Age", "600")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Loopback-only Sovereign Workbench companion")
    result.add_argument("--state-db", required=True, type=Path)
    result.add_argument("--staging-db", required=True, type=Path)
    result.add_argument("--port", type=int, default=8765)
    result.add_argument("--allow-origin", action="append", default=[])
    result.add_argument("--plan-ttl-seconds", type=int, default=300)
    result.add_argument("--token-ttl-seconds", type=int, default=8 * 60 * 60)
    result.add_argument("--rate-limit-per-minute", type=int, default=120)
    result.add_argument("--session-file", type=Path)
    return result


def _write_session_file(path: Path, session: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(session, sort_keys=True), encoding="utf-8")
    path.chmod(0o600)
    if os.name == "nt":
        username = os.environ.get("USERNAME")
        domain = os.environ.get("USERDOMAIN")
        principal = f"{domain}\\{username}" if domain and username else username
        if not principal:
            path.unlink(missing_ok=True)
            raise RuntimeError("Cannot determine the Windows user for companion session ACL")
        completed = subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{principal}:(R,W)"],
            capture_output=True, check=False, timeout=10,
        )
        if completed.returncode != 0:
            path.unlink(missing_ok=True)
            raise RuntimeError("Could not restrict the companion session file ACL")


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    token = secrets.token_urlsafe(32)
    config = CompanionConfig(
        args.state_db, args.staging_db, token, frozenset(args.allow_origin),
        plan_ttl_seconds=_bounded_int(args.plan_ttl_seconds, 30, 3600),
        token_ttl_seconds=_bounded_int(args.token_ttl_seconds, 300, 24 * 60 * 60),
        rate_limit_per_minute=_bounded_int(args.rate_limit_per_minute, 10, 600),
    )
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(CompanionService(config)))
    session = {"url": f"http://127.0.0.1:{args.port}", "token": token,
               "contract_version": CONTRACT_VERSION, "authority": "none",
               "pid": os.getpid(), "token_ttl_seconds": config.token_ttl_seconds}
    if args.session_file:
        _write_session_file(args.session_file, session)
    print(json.dumps(session), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
        if args.session_file:
            args.session_file.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
