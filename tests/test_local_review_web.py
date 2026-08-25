import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

from sovereign_workbench.local_review_web import make_server
from sovereign_workbench.review_batches import connect

from test_local_review import prepared


def request(url: str, token: str = "", payload: dict | None = None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    return urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers), timeout=3)


def running_server(database_path: Path):
    server, token = make_server(database_path, "Dominic", "fixed-test-token")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, token, f"http://127.0.0.1:{server.server_port}"


def test_browser_review_requires_token_and_does_not_embed_private_data(tmp_path: Path):
    _, database = prepared(tmp_path)
    database.close()
    server, thread, token, url = running_server(tmp_path / "review.db")
    try:
        html = request(url + "/").read().decode("utf-8")
        assert "fixed-test-token" not in html
        assert "uncertain.txt" not in html
        try:
            request(url + "/api/next")
            raise AssertionError("API request without a token should fail")
        except urllib.error.HTTPError as exc:
            assert exc.code == 401
        result = json.load(request(url + "/api/next", token))
        assert result["item"]["source_path"] == "broken.docx"
        assert result["remaining"] == 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_browser_decision_is_hash_bound_and_append_only(tmp_path: Path):
    source, database = prepared(tmp_path)
    database.close()
    before = (source / "broken.docx").read_bytes()
    server, thread, token, url = running_server(tmp_path / "review.db")
    try:
        item = json.load(request(url + "/api/next", token))["item"]
        payload = {
            "source_id": item["source_id"],
            "decision": "QUARANTINE",
            "note": "Invalid container; preserve for recovery",
            "research_question": "",
        }
        assert json.load(request(url + "/api/decision", token, payload)) == {"recorded": True}
        assert (source / "broken.docx").read_bytes() == before
        assert json.load(request(url + "/api/next", token))["item"]["source_path"] == "uncertain.txt"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
    check = connect(tmp_path / "review.db")
    assert check.execute("SELECT decision,reviewer FROM archive_review_decisions").fetchone() == ("QUARANTINE", "Dominic")
    check.close()


def test_browser_blocks_changed_source_and_unexpected_fields(tmp_path: Path):
    source, database = prepared(tmp_path)
    database.close()
    server, thread, token, url = running_server(tmp_path / "review.db")
    try:
        item = json.load(request(url + "/api/next", token))["item"]
        (source / "broken.docx").write_text("changed after admission", encoding="utf-8")
        payload = {"source_id": item["source_id"], "decision": "REJECT", "note": "changed", "research_question": ""}
        try:
            request(url + "/api/decision", token, payload)
            raise AssertionError("Changed source should fail closed")
        except urllib.error.HTTPError as exc:
            assert exc.code == 400
            assert "changed" in json.load(exc)["error"]
        payload["extra"] = "not allowed"
        try:
            request(url + "/api/decision", token, payload)
            raise AssertionError("Unexpected fields should fail closed")
        except urllib.error.HTTPError as exc:
            assert exc.code == 400
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
    check = connect(tmp_path / "review.db")
    assert check.execute("SELECT COUNT(*) FROM archive_review_decisions").fetchone()[0] == 0
    check.close()
