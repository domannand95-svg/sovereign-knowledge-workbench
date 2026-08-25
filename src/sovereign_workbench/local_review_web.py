from __future__ import annotations

import argparse
import hmac
import json
import secrets
import threading
import webbrowser
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .local_review import ReviewController, propose_research_question
from .review_batches import connect


MAX_REQUEST_BYTES = 64 * 1024
DECISIONS = {
    "APPROVE": "APPROVE",
    "RESEARCH": "NEEDS_RESEARCH",
    "QUARANTINE": "QUARANTINE",
    "REJECT": "REJECT",
}


HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sovereign Workbench — Local Review</title>
<style>
:root{font-family:Segoe UI,system-ui,sans-serif;color:#172235;background:#eef3f9}body{margin:0}.page{max-width:1050px;margin:auto;padding:28px 22px 60px}
.card{background:white;border:1px solid #d8e0ea;border-radius:14px;padding:22px;box-shadow:0 8px 30px #20334d14}.muted{color:#526174}.route{display:inline-block;background:#fff0df;color:#7b3913;padding:7px 11px;border-radius:8px;font-weight:700}
h1{font-size:1.45rem;overflow-wrap:anywhere}.box{white-space:pre-wrap;background:#f7f9fc;border:1px solid #d8e0ea;border-radius:9px;padding:14px;min-height:55px}label{display:block;font-weight:700;margin:18px 0 6px}
textarea{box-sizing:border-box;width:100%;min-height:92px;padding:12px;border:1px solid #aebac8;border-radius:9px;font:inherit}.choices{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-top:22px}
button{border:0;border-radius:10px;color:white;padding:18px 10px;font-size:1rem;font-weight:800;cursor:pointer}.approve{background:#237a4b}.research{background:#275dad}.quarantine{background:#a65d00}.reject{background:#a12a2a}
.safety{margin-top:20px;padding:13px;background:#e9f1fb;border-radius:9px}.error{background:#ffe7e7;color:#831d1d;padding:12px;border-radius:9px;margin-bottom:14px}.hidden{display:none}@media(max-width:700px){.choices{grid-template-columns:1fr 1fr}}
</style></head><body><main class="page"><div id="error" class="error hidden"></div><section class="card">
<div id="progress" class="muted">Opening local review…</div><h1 id="path"></h1><div id="route" class="route"></div>
<label>Summary</label><div id="summary" class="box"></div><label>Why it needs review</label><div id="reasons" class="box"></div>
<label for="question">Research question (editable)</label><textarea id="question"></textarea><label for="note">Your review note (editable)</label><textarea id="note">Human reviewed the candidate summary and routing reason.</textarea>
<div class="choices"><button class="approve" data-choice="APPROVE">APPROVE</button><button class="research" data-choice="RESEARCH">RESEARCH</button><button class="quarantine" data-choice="QUARANTINE">QUARANTINE</button><button class="reject" data-choice="REJECT">REJECT</button></div>
<div class="safety">This screen records review evidence only. It cannot move, delete, publish, stage, or train on files.</div></section></main>
<script>
const token=location.hash.slice(1);history.replaceState(null,"",location.pathname);let current=null;
const el=id=>document.getElementById(id);const auth={"Authorization":"Bearer "+token};
function error(message){el("error").textContent=message;el("error").classList.toggle("hidden",!message)}
async function load(){error("");const response=await fetch("/api/next",{headers:auth,cache:"no-store"});if(!response.ok)throw new Error((await response.json()).error||"Review could not be loaded");const data=await response.json();current=data.item;
 if(!current){el("progress").textContent="Review complete";el("path").textContent="No review items remain in this database.";el("route").textContent="All recorded decisions remain non-authoritative.";document.querySelectorAll("button,textarea").forEach(x=>x.disabled=true);return}
 el("progress").textContent=`File 1 of ${data.remaining}`;el("path").textContent=current.source_path;el("route").textContent=`${current.review_route} • SHA-256 ${current.source_sha256.slice(0,16)}…`;el("summary").textContent=current.model_summary||"No summary is available.";el("reasons").textContent=current.display_reasons;el("question").value=current.research_question;el("note").value="Human reviewed the candidate summary and routing reason.";
}
async function decide(choice){if(!current)return;document.querySelectorAll("button").forEach(x=>x.disabled=true);error("");try{const response=await fetch("/api/decision",{method:"POST",headers:{...auth,"Content-Type":"application/json"},body:JSON.stringify({source_id:current.source_id,decision:choice,note:el("note").value,research_question:el("question").value})});const data=await response.json();if(!response.ok)throw new Error(data.error||"Decision was blocked");await load()}catch(e){error(e.message)}finally{document.querySelectorAll("button").forEach(x=>x.disabled=false)}}
document.querySelectorAll("button[data-choice]").forEach(button=>button.addEventListener("click",()=>decide(button.dataset.choice)));load().catch(e=>error(e.message));
</script></body></html>"""


def _display_reasons(value: str) -> str:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return value
    if isinstance(decoded, list):
        return "\n".join(f"• {item}" for item in decoded)
    return str(decoded)


def make_server(state_db: Path, reviewer: str, token: str | None = None) -> tuple[HTTPServer, str]:
    access_token = token or secrets.token_urlsafe(32)
    database_path = state_db.resolve(strict=True)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return

        def _authorized(self) -> bool:
            supplied = self.headers.get("Authorization", "")
            return hmac.compare_digest(supplied, f"Bearer {access_token}")

        def _json(self, status: HTTPStatus, value: dict[str, object]) -> None:
            body = json.dumps(value).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _controller(self) -> tuple[object, ReviewController]:
            database = connect(database_path)
            return database, ReviewController(database, reviewer)

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path == "/":
                body = HTML.encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'")
                self.end_headers()
                self.wfile.write(body)
                return
            if path != "/api/next":
                self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
                return
            if not self._authorized():
                self._json(HTTPStatus.UNAUTHORIZED, {"error": "Local review access was denied"})
                return
            database, controller = self._controller()
            try:
                pending = controller.pending()
                if not pending:
                    self._json(HTTPStatus.OK, {"item": None, "remaining": 0})
                    return
                item = pending[0]
                value = asdict(item)
                value["display_reasons"] = _display_reasons(item.route_reasons)
                value["research_question"] = propose_research_question(item)
                self._json(HTTPStatus.OK, {"item": value, "remaining": len(pending)})
            finally:
                database.close()

        def do_POST(self) -> None:
            if urlparse(self.path).path != "/api/decision":
                self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
                return
            if not self._authorized():
                self._json(HTTPStatus.UNAUTHORIZED, {"error": "Local review access was denied"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > MAX_REQUEST_BYTES:
                    raise ValueError("The review request size is invalid")
                payload = json.loads(self.rfile.read(length))
                required = {"source_id", "decision", "note", "research_question"}
                if not isinstance(payload, dict) or set(payload) != required:
                    raise ValueError("The review request fields are invalid")
                if not all(isinstance(payload[key], str) for key in required):
                    raise ValueError("The review request values must be text")
                decision = DECISIONS.get(payload["decision"])
                if decision is None:
                    raise ValueError("The selected review decision is invalid")
                database, controller = self._controller()
                try:
                    pending = controller.pending()
                    if not pending or pending[0].source_id != payload["source_id"]:
                        raise ValueError("The pending review item changed; reload before deciding")
                    question = payload["research_question"] if decision == "NEEDS_RESEARCH" else ""
                    inserted = controller.accept(pending[0], decision, note=payload["note"], research_question=question)
                    if inserted != 1:
                        raise ValueError("This exact review decision was already recorded")
                finally:
                    database.close()
                self._json(HTTPStatus.OK, {"recorded": True})
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    return HTTPServer(("127.0.0.1", 0), Handler), access_token


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="One-file-at-a-time local archive review")
    result.add_argument("--state-db", required=True, type=Path)
    result.add_argument("--reviewer", required=True)
    result.add_argument("--no-browser", action="store_true", help=argparse.SUPPRESS)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if not args.reviewer.strip():
        raise ValueError("A human reviewer name is required")
    server, token = make_server(args.state_db, args.reviewer)
    url = f"http://127.0.0.1:{server.server_port}/#{token}"
    print(f"Local review is ready at {url}")
    print("Keep this window open while reviewing. Press Ctrl+C here when finished.")
    if not args.no_browser:
        threading.Timer(0.2, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
