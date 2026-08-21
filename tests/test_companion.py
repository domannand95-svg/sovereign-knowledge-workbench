import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from sovereign_workbench.companion import CompanionConfig, CompanionService, make_handler
from http.server import ThreadingHTTPServer
from threading import Thread


@pytest.fixture
def companion(tmp_path: Path):
    calls = []
    def authorize(proposal):
        calls.append(proposal)
        return {"operation": proposal["operation"], "target": proposal["target"],
                "proposal_sha256": "verified-internally"}
    config = CompanionConfig(tmp_path / "jobs.db", tmp_path / "staging.db", "test-token",
                             frozenset({"https://app.base44.com"}))
    service = CompanionService(config, authorizer=authorize)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    yield service, calls, f"http://127.0.0.1:{server.server_port}"
    server.shutdown(); thread.join(); server.server_close()


def request(url, path, *, method="GET", body=None, token="test-token", origin="https://app.base44.com"):
    data = None if body is None else json.dumps(body).encode()
    headers = {"Authorization": f"Bearer {token}", "Origin": origin}
    if data is not None: headers["Content-Type"] = "application/json"
    with urlopen(Request(url + path, data=data, headers=headers, method=method)) as response:
        return response.status, json.loads(response.read())


def test_health_and_reviews_are_local_authenticated_contracts(companion):
    _, _, url = companion
    assert request(url, "/v1/health")[1]["loopback_only"] is True
    assert request(url, "/v1/reviews")[1] == {"integrity":{"candidates":0,"decisions":0,"valid":True},"items":[]}
    with pytest.raises(HTTPError) as missing:
        request(url, "/v1/health", token="wrong")
    assert missing.value.code == 401


def test_origin_is_fail_closed(companion):
    _, _, url = companion
    with pytest.raises(HTTPError) as denied:
        request(url, "/v1/health", origin="https://attacker.example")
    assert denied.value.code == 403


def test_execution_cannot_accept_browser_supplied_receipt(companion, tmp_path: Path):
    service, calls, url = companion
    source = tmp_path / "source.txt"; source.write_text("synthetic", encoding="utf-8")
    staging_root = tmp_path / "staging"; staging_root.mkdir()
    _, plan = request(url, "/v1/staging/plans", method="POST",
                      body={"source_path":str(source),"staging_root":str(staging_root)})
    with pytest.raises(HTTPError) as forged:
        request(url, f"/v1/staging/plans/{plan['plan_id']}/execute", method="POST",
                body={"receipt":{"authorized":True}})
    assert forged.value.code == 400
    assert calls == []
    assert not Path(plan["staged_path"]).exists()
    status, result = request(url, f"/v1/staging/plans/{plan['plan_id']}/execute", method="POST", body={})
    assert status == 200 and result["status"] == "staged"
    assert calls == [{"operation":"stage_copy","target":plan["staged_path"],"report_sha256":plan["plan_id"]}]
    assert source.read_text(encoding="utf-8") == "synthetic"


def test_unknown_plan_cannot_execute(companion):
    _, calls, url = companion
    with pytest.raises(HTTPError) as unknown:
        request(url, "/v1/staging/plans/not-held/execute", method="POST", body={})
    assert unknown.value.code == 404 and calls == []
