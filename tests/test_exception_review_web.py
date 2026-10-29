import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

from sovereign_workbench.exception_review_web import make_server
from sovereign_workbench.intake import scan_files
from sovereign_workbench.review_batches import admit, connect, create_next, register_intake_root
from sovereign_workbench.section_review import build_section_proposals


def _prepared(tmp_path: Path):
    root=tmp_path/"archive"; root.mkdir(); source=root/"Appendix K.txt"
    source.write_text("GOVERNANCE:\nPolicy quorum authority execution capability authorization.\nENERGY AND COMPUTE:\nPlasma reactor power thermal energy neuromorphic compute photonic processor.",encoding="utf-8")
    database=connect(tmp_path/"state.db"); register_intake_root(database,root,purpose="test",allowed_suffixes={".txt"}); admit(database,root,scan_files(root)); create_next(database); build_section_proposals(database); database.close(); return source


def _request(url: str, token: str="", payload=None):
    headers={"Authorization":f"Bearer {token}"} if token else {}; data=None
    if payload is not None: data=json.dumps(payload).encode(); headers["Content-Type"]="application/json"
    return urllib.request.urlopen(urllib.request.Request(url,data=data,headers=headers),timeout=3)


def test_exception_browser_is_token_bound_and_records_map_decision(tmp_path: Path):
    source=_prepared(tmp_path); before=source.read_bytes(); server,token=make_server(tmp_path/"state.db","Reviewer","fixed")
    thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start(); url=f"http://127.0.0.1:{server.server_port}"
    try:
        assert "Document Map Review" in _request(url+"/").read().decode()
        try: _request(url+"/api/next"); raise AssertionError("missing token accepted")
        except urllib.error.HTTPError as exc: assert exc.code==401
        item=json.load(_request(url+"/api/next",token))["item"]
        assert item["section_evidence"][0]["excerpt"]
        assert "heading" in item["section_evidence"][0]
        assert "exclusion_warnings" in item
        assert "concept_directives" in item
        payload={"source_id":item["source_id"],"source_sha256":item["source_sha256"],"map_sha256":item["map_sha256"],"decision":"NEEDS_RESEARCH","reason":"Research the cross-family boundary"}
        assert json.load(_request(url+"/api/decision",token,payload))=={"recorded":True}
        assert source.read_bytes()==before
        assert json.load(_request(url+"/api/next",token))["item"] is None
    finally: server.shutdown(); server.server_close(); thread.join(timeout=3)
    with connect(tmp_path/"state.db") as database:
        assert database.execute("SELECT decision,reviewer FROM archive_document_map_decisions").fetchone()==("NEEDS_RESEARCH","Reviewer")


def test_exception_browser_fails_closed_if_source_changes(tmp_path: Path):
    source=_prepared(tmp_path); server,token=make_server(tmp_path/"state.db","Reviewer","fixed")
    thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start(); url=f"http://127.0.0.1:{server.server_port}"
    try:
        item=json.load(_request(url+"/api/next",token))["item"]; source.write_text("changed",encoding="utf-8")
        payload={"source_id":item["source_id"],"source_sha256":item["source_sha256"],"map_sha256":item["map_sha256"],"decision":"MAP_ACCEPTED","reason":"reviewed"}
        try: _request(url+"/api/decision",token,payload); raise AssertionError("changed source accepted")
        except urllib.error.HTTPError as exc: assert exc.code==400 and "changed" in json.load(exc)["error"]
    finally: server.shutdown(); server.server_close(); thread.join(timeout=3)
