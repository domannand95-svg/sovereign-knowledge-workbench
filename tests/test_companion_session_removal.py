"""Companion shutdown removes only the session file it published; stop script is guarded."""
from pathlib import Path
import json
import re

from sovereign_workbench import companion
from sovereign_workbench.session_publication import identity_of

REPO = Path(__file__).resolve().parents[1]


def _run(tmp_path, monkeypatch, during_serve=None):
    session = tmp_path / "session.json"
    events = []

    def write(path, value):
        path.write_text(json.dumps(value))
        return identity_of(path)

    class Server:
        def __init__(self, address, handler): events.append("constructed")
        def serve_forever(self):
            if during_serve:
                during_serve(session)
            raise KeyboardInterrupt()
        def server_close(self): events.append("closed")

    monkeypatch.setattr(companion, "_write_session_file", write)
    monkeypatch.setattr(companion, "ThreadingHTTPServer", Server)
    monkeypatch.setattr(companion, "CompanionService", lambda config: config)
    monkeypatch.setattr(companion, "make_handler", lambda service: object())
    args = ["--state-db", str(tmp_path / "state.db"), "--staging-db", str(tmp_path / "stage.db"),
            "--session-file", str(session)]
    assert companion.main(args) == 0
    return session, events


def test_shutdown_removes_the_session_file_it_published(tmp_path, monkeypatch):
    session, events = _run(tmp_path, monkeypatch)
    assert events[-1] == "closed" and not session.exists()


def test_shutdown_keeps_a_session_file_replaced_while_running(tmp_path, monkeypatch, capsys):
    def replace(session):
        session.unlink()
        session.write_text("replacement-record")
    session, _ = _run(tmp_path, monkeypatch, replace)
    assert session.read_text() == "replacement-record"
    assert "left in place (kept-replaced)" in capsys.readouterr().err


def test_stop_script_removes_the_session_file_only_when_its_bytes_are_unchanged():
    text = (REPO / "scripts" / "stop-companion.ps1").read_text(encoding="ascii")
    removals = [m.start() for m in re.finditer(r"Remove-Item", text)]
    assert len(removals) == 1
    guard = text.rfind("if ($unchanged)", 0, removals[0])
    assert guard != -1 and text[guard:removals[0]].count("\n") == 0
    assert text.index("ReadAllBytes($sessionPath)") < text.index("Stop-Process") < text.index("$afterHash")
    assert "ReparsePoint" in text and "left in place" in text
