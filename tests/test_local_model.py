from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from sovereign_workbench.local_model import LocalModelConfig, ModelError, classify_with_local_model
from sovereign_workbench.model import FileRecord


def record(text: str) -> FileRecord:
    return FileRecord("sample.txt", "0" * 64, len(text), "text/plain", 0, text, "extracted")


def test_local_model_content_is_bounded_by_configuration():
    config = LocalModelConfig("http://local.invalid", "bounded-model", max_content_chars=1_000)
    envelope = {"choices": [{"message": {"content": json.dumps({
        "module": "research", "confidence": 0.8, "labels": [], "summary": "bounded"
    })}}]}
    with patch("sovereign_workbench.local_model.urllib.request.urlopen") as opener:
        opener.return_value.__enter__.return_value.read.return_value = json.dumps(envelope).encode()
        result = classify_with_local_model(record("x" * 2_000), config)
    request_body = json.loads(opener.call_args.args[0].data)
    prompt = json.loads(request_body["messages"][1]["content"])
    assert len(prompt["content"]) == 1_000
    assert result.source == "local_model_candidate"


@pytest.mark.parametrize("ceiling", [999, 20_001])
def test_local_model_content_ceiling_fails_closed(ceiling: int):
    config = LocalModelConfig("http://local.invalid", "bounded-model", max_content_chars=ceiling)
    with pytest.raises(ModelError, match="content ceiling"):
        classify_with_local_model(record("content"), config)
