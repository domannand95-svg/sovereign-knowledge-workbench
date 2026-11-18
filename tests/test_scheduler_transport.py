"""Scheduler model calls use the same loopback-only transport as the local classifier."""
from __future__ import annotations

import json

import pytest

from sovereign_workbench.scheduler import ModelWorker, SchedulerError, _invoke, load_workers, run_schedule
from test_model_transport import REJECTED, _reply, connections, server_factory  # noqa: F401  (fixtures)

TASK = {"task_id": "t1", "role": "review", "prompt": "SECRET-TASK-TEXT"}
PROPOSAL = {"choices": [{"message": {"content": json.dumps({"verdict": "ok"})}}]}


def _worker(endpoint: str, *, max_bytes: int = 1_048_576, timeout: int = 5) -> ModelWorker:
    return ModelWorker("w", "m", ("review",), endpoint, timeout, max_bytes)


def _config(tmp_path, endpoint: str):
    path = tmp_path / "models.json"
    path.write_text(json.dumps({"contract_version": "sovereign.workbench.models.v1", "workers": [
        {"worker_id": "w", "model": "m", "roles": ["review"], "endpoint": endpoint}]}), encoding="utf-8")
    return path


@pytest.mark.parametrize("endpoint", REJECTED)
def test_non_loopback_worker_is_refused_at_load_and_at_call_without_any_connection(endpoint, tmp_path, connections):
    with pytest.raises(SchedulerError):
        load_workers(_config(tmp_path, endpoint))
    with pytest.raises(SchedulerError):
        _invoke(TASK, _worker(endpoint))
    assert connections == []


def test_default_and_example_endpoints_still_load(tmp_path):
    path = tmp_path / "models.json"
    path.write_text(json.dumps({"contract_version": "sovereign.workbench.models.v1",
                                "workers": [{"worker_id": "w", "model": "m", "roles": ["review"]}]}), encoding="utf-8")
    assert load_workers(path)[1][0].endpoint == "http://127.0.0.1:11434/v1/chat/completions"
    load_workers(__import__("pathlib").Path("config/models.ollama.example.json"))


def test_loopback_worker_returns_candidate_with_no_authority(server_factory, connections):
    server = server_factory(_reply(200, json.dumps(PROPOSAL).encode()))
    result = _invoke(TASK, _worker(f"http://127.0.0.1:{server.port}/v1/chat/completions"))
    assert result["status"] == "candidate" and result["authority"] == "none"
    assert result["proposal"] == {"verdict": "ok"}
    assert server.requests == ["/v1/chat/completions"] and connections


def test_scheduler_ignores_proxy_environment(server_factory, monkeypatch):
    proxy = server_factory(_reply(200, json.dumps(PROPOSAL).encode()))
    model = server_factory(_reply(200, json.dumps(PROPOSAL).encode()))
    for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(name, f"http://127.0.0.1:{proxy.port}")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    _invoke(TASK, _worker(f"http://127.0.0.1:{model.port}/v1"))
    assert proxy.requests == [] and model.requests == ["/v1"]


def test_scheduler_does_not_follow_redirects(server_factory):
    elsewhere = server_factory(_reply(200, json.dumps(PROPOSAL).encode()))
    redirector = server_factory(_reply(302, headers={"Location": f"http://127.0.0.1:{elsewhere.port}/v1"}))
    with pytest.raises(SchedulerError, match="redirects are not followed"):
        _invoke(TASK, _worker(f"http://127.0.0.1:{redirector.port}/v1"))
    assert elsewhere.requests == []


def test_worker_byte_limit_is_enforced(server_factory):
    server = server_factory(_reply(200, json.dumps(PROPOSAL).encode()))
    with pytest.raises(SchedulerError, match="size limit"):
        _invoke(TASK, _worker(f"http://127.0.0.1:{server.port}/v1", max_bytes=16))


def test_failure_is_isolated_and_does_not_echo_reply_content(server_factory):
    server = server_factory(_reply(200, b'{"choices": "SECRET-REPLY-TEXT"}'))
    results = run_schedule([TASK], [_worker(f"http://127.0.0.1:{server.port}/v1")], max_concurrency=1)
    assert results[0]["status"] == "failed" and results[0]["authority"] == "none"
    assert "SECRET" not in results[0]["error"]


def test_stored_job_with_non_loopback_endpoint_fails_without_sending(tmp_path, connections):
    from sovereign_workbench.scheduler import connect_schedule, enqueue_schedule, run_pending_schedule
    with connect_schedule(tmp_path / "s.db") as database:
        enqueue_schedule(database, [TASK], [_worker("http://example.com/v1")])
        connections.clear()
        assert run_pending_schedule(database, max_concurrency=1) == {"completed": 0, "failed": 1}
    assert connections == []
