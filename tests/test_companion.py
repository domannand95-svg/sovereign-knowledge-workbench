import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Event
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from sovereign_workbench.companion import CompanionConfig, CompanionService, make_handler
from http.server import ThreadingHTTPServer
from threading import Thread


@contextmanager
def running(service):
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown(); thread.join(); server.server_close()


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
    with running(service) as url:
        yield service, calls, url


def request(url, path, *, method="GET", body=None, token="test-token", origin="https://app.base44.com"):
    data = None if body is None else json.dumps(body).encode()
    headers = {"Authorization": f"Bearer {token}", "Origin": origin,
               "X-Companion-Contract": "sovereign.workbench.companion.v1"}
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


@pytest.mark.parametrize("origin", [
    "https://app.base44.com\r\nX-Injected: true",
    "https://user:password@app.base44.com",
    "https://app.base44.com/path",
    "file://app.base44.com",
])
def test_config_rejects_noncanonical_or_header_injection_origins(tmp_path: Path, origin: str):
    with pytest.raises(ValueError, match="canonical HTTP"):
        CompanionConfig(tmp_path / "jobs.db", tmp_path / "staging.db", "test-token",
                        frozenset({origin}))


def test_contract_mismatch_and_expired_token_fail_closed(tmp_path: Path):
    config = CompanionConfig(tmp_path / "jobs.db", tmp_path / "staging.db", "test-token",
                             frozenset({"https://app.base44.com"}), token_ttl_seconds=0)
    with running(CompanionService(config)) as url:
        with pytest.raises(HTTPError) as expired:
            request(url, "/v1/health")
        assert expired.value.code == 401
    config = CompanionConfig(tmp_path / "jobs2.db", tmp_path / "staging2.db", "test-token",
                             frozenset({"https://app.base44.com"}))
    with running(CompanionService(config)) as url:
        raw = Request(url + "/v1/health", headers={"Authorization":"Bearer test-token",
                      "Origin":"https://app.base44.com"})
        with pytest.raises(HTTPError) as mismatch:
            urlopen(raw)
        assert mismatch.value.code == 412


def test_request_throttling_is_fail_closed(tmp_path: Path):
    config = CompanionConfig(tmp_path / "jobs.db", tmp_path / "staging.db", "test-token",
                             frozenset({"https://app.base44.com"}), rate_limit_per_minute=1)
    with running(CompanionService(config)) as url:
        assert request(url, "/v1/health")[0] == 200
        with pytest.raises(HTTPError) as limited:
            request(url, "/v1/health")
        assert limited.value.code == 429


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


def test_contract_forbids_browser_receipts_and_matches_routes():
    contract = json.loads((Path(__file__).parents[1] / "contracts" / "companion-v1.openapi.json").read_text())
    assert contract["x-governance"] == {
        "contractVersion":"sovereign.workbench.companion.v1",
        "authority":"none", "browserReceiptsAllowed":False,
        "executionAuthorization":"local-companion-only", "sourceMutationAllowed":False,
    }
    empty = contract["components"]["schemas"]["EmptyAuthorityRequest"]
    assert empty["additionalProperties"] is False and empty["maxProperties"] == 0
    assert set(contract["paths"]) == {"/v1/health", "/v1/reviews", "/v1/staging/status",
                                      "/v1/staging/plans", "/v1/staging/plans/{plan_id}/execute",
                                      "/v1/staging/plans/{plan_id}/rollback"}


def test_plan_is_one_shot_audited_and_reversible(companion, tmp_path: Path):
    _, _, url = companion
    source = tmp_path / "demo.txt"; source.write_text("synthetic-only", encoding="utf-8")
    root = tmp_path / "staging"; root.mkdir()
    _, plan = request(url, "/v1/staging/plans", method="POST",
                      body={"source_path":str(source), "staging_root":str(root)})
    request(url, f"/v1/staging/plans/{plan['plan_id']}/execute", method="POST", body={})
    with pytest.raises(HTTPError) as replay:
        request(url, f"/v1/staging/plans/{plan['plan_id']}/execute", method="POST", body={})
    assert replay.value.code == 409
    _, rolled_back = request(url, f"/v1/staging/plans/{plan['plan_id']}/rollback",
                             method="POST", body={})
    assert rolled_back["status"] == "rolled_back"
    assert source.read_text(encoding="utf-8") == "synthetic-only"
    assert not Path(plan["staged_path"]).exists()
    with connect_db(companion[0].config.staging_db) as database:
        events = [row[0] for row in database.execute("SELECT event_type FROM companion_audit ORDER BY sequence")]
    assert events == ["plan_created", "execution_completed", "rollback_completed"]


def connect_db(path: Path):
    import sqlite3
    return sqlite3.connect(path)


def test_expired_plan_fails_before_authorization(tmp_path: Path):
    calls = []
    service = CompanionService(CompanionConfig(tmp_path / "jobs.db", tmp_path / "staging.db", "token",
                                                plan_ttl_seconds=0), authorizer=lambda proposal: calls.append(proposal))
    source = tmp_path / "source.txt"; source.write_text("synthetic", encoding="utf-8")
    root = tmp_path / "root"; root.mkdir()
    plan = service.plan({"source_path":str(source), "staging_root":str(root)})
    with pytest.raises(Exception, match="expired"):
        service.execute(plan["plan_id"], {})
    assert calls == [] and not Path(plan["staged_path"]).exists()


def test_concurrent_execution_claims_plan_once(tmp_path: Path):
    entered, release = Event(), Event()
    def authorize(proposal):
        entered.set(); release.wait(2)
        return {"operation":proposal["operation"], "target":proposal["target"],
                "proposal_sha256":"verified"}
    service = CompanionService(CompanionConfig(tmp_path / "jobs.db", tmp_path / "staging.db", "token"),
                               authorizer=authorize)
    source = tmp_path / "source.txt"; source.write_text("synthetic", encoding="utf-8")
    root = tmp_path / "root"; root.mkdir()
    plan = service.plan({"source_path":str(source), "staging_root":str(root)})
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(service.execute, plan["plan_id"], {})
        assert entered.wait(1)
        second = pool.submit(service.execute, plan["plan_id"], {})
        with pytest.raises(Exception, match="not available"):
            second.result()
        release.set()
        assert first.result()["status"] == "staged"
