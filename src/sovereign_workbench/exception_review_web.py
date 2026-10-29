from __future__ import annotations

import argparse
import hmac
import json
import secrets
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .review_batches import connect
from .section_review import decide_document_map, exception_queue


HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sovereign Workbench — Document Map Review</title><style>
:root{font-family:Segoe UI,system-ui,sans-serif;color:#172235;background:#eef3f9}body{margin:0}.page{max-width:1100px;margin:auto;padding:28px 20px}.card{background:#fff;border:1px solid #d8e0ea;border-radius:14px;padding:22px;box-shadow:0 8px 30px #20334d14}h1{overflow-wrap:anywhere;font-size:1.4rem}.muted{color:#58677a}.topics{display:flex;gap:8px;flex-wrap:wrap}.pill{background:#e9f1fb;padding:7px 10px;border-radius:9px;font-weight:700}.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin:18px 0}.box{background:#f7f9fc;border:1px solid #d8e0ea;border-radius:10px;padding:14px;white-space:pre-wrap}.evidence{border-left:5px solid #275dad;margin:10px 0}.warning{background:#fff2d8;border-color:#c17300}.choices{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:18px}button{border:0;border-radius:10px;color:#fff;padding:17px;font-weight:800;cursor:pointer}.accept{background:#237a4b}.research{background:#275dad}.reclassify{background:#a65d00}textarea{box-sizing:border-box;width:100%;min-height:85px;padding:12px;border:1px solid #aebac8;border-radius:9px;font:inherit}.safety{margin-top:18px;padding:12px;background:#e9f1fb;border-radius:9px}.error{background:#ffe7e7;color:#831d1d;padding:12px;border-radius:9px}.hidden{display:none}@media(max-width:700px){.grid,.choices{grid-template-columns:1fr}}
</style></head><body><main class="page"><div id="error" class="error hidden"></div><section class="card"><div id="progress" class="muted">Opening exception review…</div><h1 id="path"></h1><div id="hash" class="muted"></div><h2>What the Workbench thinks this is</h2><div id="candidate" class="box"></div><div id="warnings" class="box warning hidden"></div><div id="directives" class="box hidden"></div><h2>Readable evidence from the file</h2><div id="evidence"></div><h2>Topic coverage</h2><div id="topics" class="topics"></div><div class="grid"><div class="box"><b>Cross-family sections</b><div id="cross"></div></div><div class="box"><b>Low-confidence routing</b><div id="low"></div></div><div class="box"><b>Unresolved relationships</b><div id="relations"></div></div><div class="box"><b>Contradictory claims</b><div id="contradictions"></div></div></div><div class="box"><b>How to decide</b><br>ACCEPT MAP: the displayed topic split is reasonable; this does not certify claims as true.<br>RESEARCH: the topic is useful, but important claims need outside evidence.<br>RECLASSIFY: it belongs elsewhere, is personal/creative, or the proposed topic split is wrong.</div><label><b>Your reason</b></label><textarea id="reason">I reviewed the displayed summary, excerpts, topic routes, and warnings.</textarea><div class="choices"><button class="accept" data-decision="MAP_ACCEPTED">ACCEPT MAP</button><button class="research" data-decision="NEEDS_RESEARCH">RESEARCH</button><button class="reclassify" data-decision="RECLASSIFY">RECLASSIFY</button></div><div class="safety">Accepting this map records review evidence only. It does not approve every underlying claim, move files, publish content, allocate training data, or start training.</div></section></main><script>
const token=location.hash.slice(1);history.replaceState(null,"",location.pathname);let current=null;const el=id=>document.getElementById(id);const auth={"Authorization":"Bearer "+token};const lines=x=>x.length?x.map(v=>"• "+v).join("\\n"):"None";const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));function err(x){el("error").textContent=x;el("error").classList.toggle("hidden",!x)}
async function load(){err("");const r=await fetch('/api/next',{headers:auth,cache:'no-store'});const d=await r.json();if(!r.ok)throw Error(d.error);current=d.item;if(!current){el('progress').textContent='Review complete';el('path').textContent='No document-map exceptions remain.';document.querySelectorAll('button,textarea').forEach(x=>x.disabled=true);return}el('progress').textContent=`Document 1 of ${d.remaining}`;el('path').textContent=current.relative_path;el('hash').textContent=`Map ${current.map_sha256.slice(0,16)}… • Source ${current.source_sha256.slice(0,16)}…`;const c=current.candidate;el('candidate').textContent=c?`Summary: ${c.summary}\\nProposed topic: ${c.topic}\\nMaturity: ${c.maturity}\\nAuthority: ${c.authority}\\nReason: ${c.reason}`:'No reliable file-level summary was produced. Use the excerpts below and choose RESEARCH or RECLASSIFY if the map is unclear.';const warnings=current.exclusion_warnings||[];el('warnings').classList.toggle('hidden',!warnings.length);el('warnings').textContent=warnings.length?'EXCLUDED FROM TECHNICAL CORPUS\\n'+lines(warnings.map(x=>`${x.marker} — ${x.heading}: ${x.reason}`)):'';const directives=current.concept_directives||[];el('directives').classList.toggle('hidden',!directives.length);el('directives').textContent=directives.length?'REVIEW DISPOSITIONS\\n'+lines(directives.map(x=>`${x.disposition} — ${x.concept} — ${x.heading}: ${x.reason}`)):'';el('evidence').innerHTML=(current.section_evidence||[]).map(x=>`<div class="box evidence"><b>${esc(x.heading)}</b><br><span class="muted">Route: ${esc(x.topic)} • confidence ${esc(x.confidence??'not scored')} • ${esc(x.reason)}</span><br><br>${esc(x.excerpt)}</div>`).join('')||'<div class="box warning">No readable excerpt is available. Do not accept this map without evidence.</div>';el('topics').innerHTML=Object.entries(current.topic_coverage).map(([k,v])=>`<span class="pill">${esc(k)}: ${esc(v)} sections</span>`).join('');el('cross').textContent=lines(current.exceptions.cross_family.map(x=>`${x.heading} → ${x.topics.join(', ')}`));const low=current.exceptions.low_confidence;el('low').textContent=`${low.count} total\\n`+lines(low.examples.map(x=>`${x.heading} → ${x.topic} (${x.confidence})`));el('relations').textContent=lines(current.exceptions.unresolved_relationships.map(x=>`${x.type}: ${x.reason}`));el('contradictions').textContent=String(current.exceptions.contradictory_claim_count)}
async function decide(decision){if(!current)return;document.querySelectorAll('button').forEach(x=>x.disabled=true);try{const r=await fetch('/api/decision',{method:'POST',headers:{...auth,'Content-Type':'application/json'},body:JSON.stringify({source_id:current.source_id,source_sha256:current.source_sha256,map_sha256:current.map_sha256,decision,reason:el('reason').value})});const d=await r.json();if(!r.ok)throw Error(d.error);await load()}catch(e){err(e.message)}finally{document.querySelectorAll('button').forEach(x=>x.disabled=false)}}document.querySelectorAll('button').forEach(x=>x.onclick=()=>decide(x.dataset.decision));load().catch(e=>err(e.message));
</script></body></html>"""


def make_server(state_db: Path, reviewer: str, token: str | None = None) -> tuple[HTTPServer, str]:
    access_token = token or secrets.token_urlsafe(32); database_path = state_db.resolve(strict=True)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None: return
        def authorized(self) -> bool:
            return hmac.compare_digest(self.headers.get("Authorization", ""), f"Bearer {access_token}")
        def send_json(self, status: HTTPStatus, value: dict[str, object]) -> None:
            body=json.dumps(value).encode(); self.send_response(status); self.send_header("Content-Type","application/json"); self.send_header("Content-Length",str(len(body))); self.send_header("Cache-Control","no-store"); self.end_headers(); self.wfile.write(body)
        def do_GET(self) -> None:
            path=urlparse(self.path).path
            if path=="/":
                body=HTML.encode(); self.send_response(200); self.send_header("Content-Type","text/html; charset=utf-8"); self.send_header("Content-Length",str(len(body))); self.send_header("Content-Security-Policy","default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'"); self.end_headers(); self.wfile.write(body); return
            if path!="/api/next": self.send_json(HTTPStatus.NOT_FOUND,{"error":"Not found"}); return
            if not self.authorized(): self.send_json(HTTPStatus.UNAUTHORIZED,{"error":"Access denied"}); return
            with connect(database_path) as database:
                queue=exception_queue(database); self.send_json(HTTPStatus.OK,{"item":queue[0] if queue else None,"remaining":len(queue)})
        def do_POST(self) -> None:
            if urlparse(self.path).path!="/api/decision": self.send_json(HTTPStatus.NOT_FOUND,{"error":"Not found"}); return
            if not self.authorized(): self.send_json(HTTPStatus.UNAUTHORIZED,{"error":"Access denied"}); return
            try:
                length=int(self.headers.get("Content-Length","0")); payload=json.loads(self.rfile.read(length))
                required={"source_id","source_sha256","map_sha256","decision","reason"}
                if not isinstance(payload,dict) or set(payload)!=required or not all(isinstance(payload[x],str) for x in required): raise ValueError("Invalid decision fields")
                with connect(database_path) as database:
                    queue=exception_queue(database)
                    if not queue or queue[0]["source_id"]!=payload["source_id"]: raise ValueError("Review queue changed; reload")
                    decide_document_map(database,payload["source_id"],payload["source_sha256"],payload["map_sha256"],payload["decision"],reviewer,payload["reason"])
                self.send_json(HTTPStatus.OK,{"recorded":True})
            except (ValueError,OSError,json.JSONDecodeError) as exc: self.send_json(HTTPStatus.BAD_REQUEST,{"error":str(exc)})
    return HTTPServer(("127.0.0.1",0),Handler),access_token


def main(argv: list[str] | None = None) -> int:
    parser=argparse.ArgumentParser(description="Local exception-focused document-map review")
    parser.add_argument("--state-db",required=True,type=Path); parser.add_argument("--reviewer",required=True); parser.add_argument("--no-browser",action="store_true")
    args=parser.parse_args(argv); server,token=make_server(args.state_db,args.reviewer); url=f"http://127.0.0.1:{server.server_port}/#{token}"; print(f"Document-map review is ready at {url}")
    if not args.no_browser: threading.Timer(.2,webbrowser.open,args=(url,)).start()
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()
    return 0


if __name__ == "__main__": raise SystemExit(main())
