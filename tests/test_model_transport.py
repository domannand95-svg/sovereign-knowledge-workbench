"""Loopback-only model transport: adversarial endpoints, zero-send proof, proxy and redirect handling."""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
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
    "http://127.0.0.1:0/v1",
    "http://127.0.0.1:/v1",
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


_AUDITED = ("socket.connect", "socket.sendto", "socket.sendmsg", "socket.getaddrinfo", "socket.getnameinfo",
            "socket.gethostbyname", "socket.gethostbyname_ex", "socket.gethostbyaddr", "subprocess.Popen",
            "os.system", "os.posix_spawn", "os.spawn", "os.exec", "os.fork")
# Not observable this way: sends on an already-connected socket that was inherited or duplicated.
# The code under test never receives such a socket.
_RECORDING: list[list[tuple[str, object]]] = []


def _audit(event, args):
    if _RECORDING and event in _AUDITED:
        _RECORDING[-1].append((event, args))


sys.addaudithook(_audit)  # audit hooks cannot be removed; it records only while a test is recording


@pytest.fixture
def connections():
    """Record every network-relevant event: connect (incl. connect_ex), sendto, sendmsg, DNS, subprocess."""
    events: list[tuple[str, object]] = []
    _RECORDING.append(events)
    try:
        yield events
    finally:
        _RECORDING.remove(events)


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


def test_meta_recorder_sees_routes_the_old_recorder_missed(connections):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0)); listener.listen(1)
        port = listener.getsockname()[1]
        with socket.socket() as client:
            client.connect_ex(("127.0.0.1", port))
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
            udp.sendto(b"x", ("127.0.0.1", port))
    events = {event for event, _ in connections}
    assert {"socket.connect", "socket.sendto"} <= events


def test_meta_recorder_sees_dns_and_process_routes(connections):
    socket.getaddrinfo("127.0.0.1", 9)
    socket.getnameinfo(("127.0.0.1", 9), 0)
    import subprocess
    subprocess.run([sys.executable, "-c", "pass"], check=True)
    os.system(f'"{sys.executable}" -c pass')
    events = {event for event, _ in connections}
    assert {"socket.getaddrinfo", "socket.getnameinfo", "subprocess.Popen", "os.system"} <= events


@pytest.mark.skipif(not hasattr(socket.socket, "sendmsg"), reason="sendmsg is POSIX only")
def test_meta_recorder_sees_sendmsg(connections):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
        udp.sendmsg([b"x"], [], 0, ("127.0.0.1", 9))
    assert "socket.sendmsg" in {event for event, _ in connections}


TRICKLE_HEADERS = {
    "keep-alive": b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\n\r\n",
    "close": b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\nConnection: close\r\n\r\n",
    "http-1.0": b"HTTP/1.0 200 OK\r\nContent-Length: 1000\r\n\r\n",
    "no-length": b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n",
    "chunked-close": b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n1000\r\n",
}


def _trickle(chunks: list[bytes], delay: float, head: bytes = TRICKLE_HEADERS["keep-alive"]):
    def handler(request):
        request.wfile.write(head)
        request.wfile.flush()
        for chunk in chunks:
            time.sleep(delay)
            try:
                request.wfile.write(chunk); request.wfile.flush()
            except OSError:
                return
    return handler


@pytest.mark.parametrize("framing", sorted(TRICKLE_HEADERS))
def test_slow_reply_is_bounded_by_total_time(server_factory, framing):
    # 10 s of trickle; each read is quick. Replies that close the connection are included because
    # http.client drops its socket reference for them, which once let them outlive the deadline.
    server = server_factory(_trickle([b"x"] * 40, 0.25, TRICKLE_HEADERS[framing]))
    started = time.monotonic()
    with pytest.raises(ModelError, match="time limit"):
        classify_with_local_model(RECORD, LocalModelConfig(endpoint=f"http://127.0.0.1:{server.port}/v1",
                                                           model="m", timeout_seconds=1))
    assert time.monotonic() - started < 3


def test_continue_flood_is_bounded_by_total_time(server_factory):
    def flood(request):
        for _ in range(40):
            time.sleep(0.25)
            try:
                request.wfile.write(b"HTTP/1.1 100 Continue\r\n\r\n"); request.wfile.flush()
            except OSError:
                return
    server = server_factory(flood)
    started = time.monotonic()
    with pytest.raises(ModelError, match="time limit"):
        classify_with_local_model(RECORD, LocalModelConfig(endpoint=f"http://127.0.0.1:{server.port}/v1",
                                                           model="m", timeout_seconds=1))
    assert time.monotonic() - started < 3


@pytest.mark.parametrize("headers", [{"Transfer-Encoding": "chunked"}, {}])
def test_reply_without_length_is_still_capped(server_factory, monkeypatch, headers):
    monkeypatch.setattr(local_model, "MAX_RESPONSE_BYTES", 1024)

    def big(request):
        request.send_response(200)
        for key, value in headers.items():
            request.send_header(key, value)
        request.send_header("Connection", "close")
        request.end_headers()
        data = b"x" * 4096
        if headers:
            request.wfile.write(b"%x\r\n%s\r\n0\r\n\r\n" % (len(data), data))
        else:
            request.wfile.write(data)
    server = server_factory(big)
    with pytest.raises(ModelError, match="size limit"):
        classify_with_local_model(RECORD, LocalModelConfig(endpoint=f"http://127.0.0.1:{server.port}/v1", model="m"))


def test_malformed_reply_does_not_echo_its_content(server_factory):
    reply = {"choices": [{"message": {"content": json.dumps(
        {"module": "research", "confidence": "SECRET-DOC-TEXT", "labels": []})}}]}
    server = server_factory(_reply(200, json.dumps(reply).encode()))
    with pytest.raises(ModelError) as caught:
        classify_with_local_model(RECORD, LocalModelConfig(endpoint=f"http://127.0.0.1:{server.port}/v1", model="m"))
    assert "SECRET-DOC-TEXT" not in str(caught.value)


@pytest.mark.parametrize("status_line", [b"SECRET-DOC-TEXT 200 OK", b"HTTP/1.1 2SECRET-DOC-TEXT OK",
                                         b"HTTP/SECRET-DOC-TEXT 200 OK"])
def test_malformed_status_line_is_not_echoed(server_factory, status_line):
    def handler(request):
        request.wfile.write(status_line + b"\r\n\r\n"); request.wfile.flush()
    server = server_factory(handler)
    with pytest.raises(ModelError) as raised:
        classify_with_local_model(RECORD, LocalModelConfig(endpoint=f"http://127.0.0.1:{server.port}/v1", model="m"))
    assert "SECRET" not in str(raised.value)
    assert raised.value.__cause__ is None and raised.value.__suppress_context__  # tracebacks do not show the cause
