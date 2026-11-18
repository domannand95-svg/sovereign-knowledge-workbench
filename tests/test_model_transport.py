"""Loopback-only model transport: adversarial endpoints, zero-send proof, proxy and redirect handling."""
from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from sovereign_workbench import local_model
from sovereign_workbench.local_model import (LocalModelConfig, ModelError, classify_with_local_model,
                                             validate_endpoint)
from sovereign_workbench.model import FileRecord

RECORD = FileRecord("notes.txt", "a" * 64, 10, "text/plain", 1, "research hypothesis")
GOOD_REPLY = {"choices": [{"message": {"content": json.dumps(
    {"module": "research", "confidence": 0.8, "labels": ["x"], "summary": "ok"})}}]}

REJECTED = [
    # host names are never resolved
    "http://localhost:11434/v1/chat/completions",
    "http://example.com/v1/chat/completions",
    "http://127.0.0.1.nip.io/v1",
    # IPv4-mapped, translated, compatible and NAT64 forms of 127.0.0.1
    "http://[::ffff:127.0.0.1]:11434/v1",
    "http://[::ffff:7f00:1]:11434/v1",
    "http://[0:0:0:0:0:ffff:7f00:1]:11434/v1",
    "http://[::ffff:0:127.0.0.1]:11434/v1",
    "http://[::127.0.0.1]:11434/v1",
    "http://[64:ff9b::7f00:1]:11434/v1",
    "http://[::]:11434/v1",
    "http://[::1%25eth0]:11434/v1",
    # non-loopback and ambiguous IPv4 spellings
    "http://0.0.0.0:11434/v1",
    "http://10.0.0.1/v1",
    "http://169.254.169.254/latest/meta-data",
    "http://2130706433/v1",
    "http://127.1/v1",
    "http://0x7f.0.0.1/v1",
    "http://0177.0.0.1/v1",
    # credentials, schemes, ports, control characters
    "http://user:pass@127.0.0.1/v1",
    "http://127.0.0.1@example.com/v1",
    "ftp://127.0.0.1/v1",
    "file:///etc/passwd",
    "127.0.0.1:11434/v1",
    "http://127.0.0.1:99999/v1",
    "http://127.0.0.1:11434/v1\r\nX-Injected: 1",
    "http://127.0.0.1:11434/v1 ",
    "",
]

ACCEPTED = [
    ("http://127.0.0.1:11434/v1/chat/completions", "127.0.0.1", 11434, "/v1/chat/completions"),
    ("http://127.255.255.254/v1", "127.255.255.254", 80, "/v1"),
    ("https://127.0.0.1/v1?x=1", "127.0.0.1", 443, "/v1?x=1"),
    ("http://[::1]:8080/v1", "::1", 8080, "/v1"),
    ("http://[0:0:0:0:0:0:0:1]:8080", "::1", 8080, "/"),
]


@pytest.fixture
def connections(monkeypatch):
    """Record every outbound connection attempt made through the socket layer."""
    attempts: list[object] = []
    real_connect = socket.socket.connect
    real_create = socket.create_connection

    def connect(sock, address):
        attempts.append(address)
        return real_connect(sock, address)

    def create_connection(address, *args, **kwargs):
        attempts.append(address)
        return real_create(address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "create_connection", create_connection)
    return attempts


@pytest.mark.parametrize("endpoint", REJECTED)
def test_rejected_endpoint_fails_closed_without_any_connection(endpoint, connections):
    with pytest.raises(ModelError):
        validate_endpoint(endpoint)
    config = LocalModelConfig(endpoint=endpoint, model="m")
    with pytest.raises(ModelError):
        classify_with_local_model(RECORD, config)
    assert connections == []


@pytest.mark.parametrize("endpoint,address,port,target", ACCEPTED)
def test_literal_loopback_endpoints_are_accepted(endpoint, address, port, target):
    parsed = validate_endpoint(endpoint)
    assert (parsed.address, parsed.port, parsed.target) == (address, port, target)


class _Server:
    def __init__(self, handler):
        self.requests: list[str] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                outer.requests.append(self.path)
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                handler(self)

            do_GET = do_POST

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def _reply(status: int, body: bytes = b"", headers: dict[str, str] | None = None):
    def handler(request):
        request.send_response(status)
        for key, value in (headers or {}).items():
            request.send_header(key, value)
        request.send_header("Content-Length", str(len(body)))
        request.end_headers()
        request.wfile.write(body)
    return handler


@pytest.fixture
def server_factory():
    servers: list[_Server] = []

    def make(handler):
        server = _Server(handler)
        servers.append(server)
        return server

    yield make
    for server in servers:
        server.close()


def test_meta_connection_recorder_sees_a_real_send(server_factory, connections):
    server = server_factory(_reply(200, json.dumps(GOOD_REPLY).encode()))
    result = classify_with_local_model(
        RECORD, LocalModelConfig(endpoint=f"http://127.0.0.1:{server.port}/v1", model="m"))
    assert result.module == "research"
    assert connections, "the recorder must observe a genuine send, or the zero-send checks prove nothing"


def test_proxy_environment_is_ignored(server_factory, monkeypatch):
    proxy = server_factory(_reply(200, json.dumps(GOOD_REPLY).encode()))
    model = server_factory(_reply(200, json.dumps(GOOD_REPLY).encode()))
    for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(name, f"http://127.0.0.1:{proxy.port}")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    classify_with_local_model(RECORD, LocalModelConfig(endpoint=f"http://127.0.0.1:{model.port}/v1", model="m"))
    assert proxy.requests == [] and model.requests == ["/v1"]


def test_redirect_is_not_followed(server_factory):
    elsewhere = server_factory(_reply(200, json.dumps(GOOD_REPLY).encode()))
    redirector = server_factory(_reply(307, headers={"Location": f"http://127.0.0.1:{elsewhere.port}/v1"}))
    with pytest.raises(ModelError, match="redirects are not followed"):
        classify_with_local_model(RECORD, LocalModelConfig(endpoint=f"http://127.0.0.1:{redirector.port}/v1", model="m"))
    assert elsewhere.requests == []


def test_oversized_reply_fails_closed(server_factory, monkeypatch):
    monkeypatch.setattr(local_model, "MAX_RESPONSE_BYTES", 64)
    server = server_factory(_reply(200, json.dumps(GOOD_REPLY).encode()))
    with pytest.raises(ModelError, match="size limit"):
        classify_with_local_model(RECORD, LocalModelConfig(endpoint=f"http://127.0.0.1:{server.port}/v1", model="m"))


def test_unreachable_loopback_fails_closed():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with pytest.raises(ModelError):
        classify_with_local_model(RECORD, LocalModelConfig(endpoint=f"http://127.0.0.1:{port}/v1", model="m"))
