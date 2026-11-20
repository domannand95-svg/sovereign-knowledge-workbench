"""Malformed local-model replies and bad limits fail closed instead of crashing."""
import json

import pytest

from sovereign_workbench.cli import main
from sovereign_workbench.local_model import LocalModelConfig, ModelError, classify_with_local_model
from sovereign_workbench.model import FileRecord
from sovereign_workbench.scheduler import ModelWorker, SchedulerError, _invoke
from test_model_transport import _reply, server_factory  # noqa: F401  (fixture)

DEEP = b"[" * 200_000


def _envelope(content: str) -> bytes:
    return json.dumps({"choices": [{"message": {"content": content}}]}).encode()


REPLIES = {
    "nested envelope": DEEP,
    "nested content": _envelope("[" * 200_000),
    "huge confidence": _envelope('{"module": "Other", "confidence": 1' + "0" * 400 + ', "summary": ""}'),
}


def _record() -> FileRecord:
    return FileRecord("x.txt", "0" * 64, 4, "text/plain", 0, "text", "extracted")


@pytest.mark.parametrize("reply", REPLIES, ids=list(REPLIES))
def test_local_model_malformed_reply_is_a_model_error(server_factory, reply):  # noqa: F811
    server = server_factory(_reply(200, REPLIES[reply]))
    config = LocalModelConfig(endpoint=f"http://127.0.0.1:{server.port}/v1", model="m",
                              timeout_seconds=5, max_content_chars=1000)
    with pytest.raises(ModelError, match="malformed reply"):
        classify_with_local_model(_record(), config, allowed_modules=["Other"])


@pytest.mark.parametrize("reply", ["nested envelope", "nested content"])
def test_scheduler_malformed_reply_is_a_scheduler_error(server_factory, reply):  # noqa: F811
    server = server_factory(_reply(200, REPLIES[reply]))
    worker = ModelWorker("w", "m", ("r",), f"http://127.0.0.1:{server.port}/v1", 5)
    with pytest.raises(SchedulerError, match="malformed reply"):
        _invoke({"task_id": "t", "role": "r", "prompt": "p"}, worker)


def test_batch_create_falls_back_when_the_model_reply_is_malformed(server_factory, tmp_path,  # noqa: F811
                                                                   monkeypatch, capsys):
    server = server_factory(_reply(200, DEEP))
    monkeypatch.setenv("SKW_MODEL_ENDPOINT", f"http://127.0.0.1:{server.port}/v1")
    monkeypatch.setenv("SKW_MODEL_TIMEOUT", "5")
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.txt").write_bytes(b"Grevillea cuttings rooted under mist.\n")
    db = tmp_path / "a.db"
    assert main(["archive-intake-register", str(docs), "--state-db", str(db), "--purpose", "t",
                 "--include", ".txt"]) == 0
    capsys.readouterr()
    assert main(["archive-batch-create", str(docs), "--state-db", str(db), "--include", ".txt",
                 "--local-model"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert (result["candidates"], result["model_failures"]) == (1, 1)


@pytest.mark.parametrize("timeout", ["0", "601"])
def test_batch_create_refuses_an_invalid_model_timeout_before_processing(tmp_path, monkeypatch,
                                                                         capsys, timeout):
    monkeypatch.setenv("SKW_MODEL_TIMEOUT", timeout)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.txt").write_bytes(b"text\n")
    db = tmp_path / "a.db"
    assert main(["archive-intake-register", str(docs), "--state-db", str(db), "--purpose", "t",
                 "--include", ".txt"]) == 0
    capsys.readouterr()
    assert main(["archive-batch-create", str(docs), "--state-db", str(db), "--include", ".txt",
                 "--local-model"]) == 3
    assert "SKW_MODEL_TIMEOUT is invalid" in capsys.readouterr().err
    status = main(["archive-batch-status", "--state-db", str(db)])
    assert status == 0 and json.loads(capsys.readouterr().out)["status"]["batched"] == 0
