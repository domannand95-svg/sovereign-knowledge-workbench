"""Browser walk-through of the local review page (helper for test_review_page_browser.py)."""
import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

from sovereign_workbench.cli import main as cli
from sovereign_workbench.extract import extract_text
import sovereign_workbench.local_review_web as web
from sovereign_workbench.local_review_web import make_server


def run_review_flow(source_docs: Path, chrome: str) -> list[tuple[str, bool, str]]:
    SOURCE_DOCS = Path(source_docs)
    real_read = web.read_verified_text
    slow = {"first": True}

    def delayed_read(*args, **kwargs):
        if slow["first"]:
            slow["first"] = False
            time.sleep(2)
        return real_read(*args, **kwargs)

    web.read_verified_text = delayed_read
    try:
        return _flow(SOURCE_DOCS, chrome)
    finally:
        web.read_verified_text = real_read


def _flow(SOURCE_DOCS: Path, chrome: str):
    work = Path(tempfile.mkdtemp(prefix="review-browser-"))
    docs = work / "docs"
    shutil.copytree(SOURCE_DOCS, docs)
    db = work / "trial.db"
    include = ["--include", ".md", ".txt", ".docx", ".pdf"]
    for argv in (["archive-intake-register", str(docs), "--state-db", str(db), "--purpose", "browser test"] + include,
                 ["archive-batch-create", str(docs), "--state-db", str(db)] + include):
        assert cli(argv) == 0

    server, token = make_server(db, "Browser Test Reviewer")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/#{token}"

    checks: list[tuple[str, bool, str]] = []


    def check(name, ok, detail=""):
        checks.append((name, bool(ok), detail))


    def expected_text(name):
        text, _ = extract_text(docs / name, max_chars=10**9)
        return text.lstrip("﻿")


    def state(page):
        return page.evaluate("""() => ({
            path: document.getElementById('path').textContent,
            doc: document.getElementById('doc').textContent,
            docstatus: document.getElementById('docstatus').textContent,
            note: document.getElementById('note').value,
            question: document.getElementById('question').value,
            saved: document.getElementById('saved').classList.contains('hidden') ? '' :
                   document.getElementById('saved').textContent,
            error: document.getElementById('error').classList.contains('hidden') ? '' :
                   document.getElementById('error').textContent,
            enabled: [...document.querySelectorAll('button[data-choice]')].map(b => !b.disabled),
            summaryLabel: [...document.querySelectorAll('label')].map(l => l.textContent)
                           .find(t => t.startsWith('Workbench summary')) || '',
        })""")


    def wait_loaded(page, previous_path=None):
        page.wait_for_function(
            """prev => { const p = document.getElementById('path').textContent;
                         const s = document.getElementById('docstatus').textContent;
                         return p && p !== prev && s && s !== 'Loading…'; }""",
            arg=previous_path or "", timeout=10_000)
        return state(page)


    decided = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chrome, headless=True)
        page = browser.new_page()
        console_errors = []
        page.on("pageerror", lambda exc: console_errors.append(str(exc)))

        page.goto(url)
        page.wait_for_function("() => document.getElementById('path').textContent.length > 0")
        page.fill("#note", "typed while loading")
        early = state(page)
        check("buttons stay disabled while the document is still loading",
              early["docstatus"] in ("", "Loading…") and not any(early["enabled"]), early["docstatus"])
        page.fill("#note", "")
        first = wait_loaded(page)
        s = first
        previous = None
        # Edit one later document on disk before the page reaches it (changed-file path).
        tamper = next(n for n in ("soil-inoculant-notes.md", "nursery-watering-procedure.docx") if n != first["path"])
        (docs / tamper).write_bytes((docs / tamper).read_bytes() + b"\nedited after import\n")
        for index in range(4):
            name = s["path"]
            if name == tamper:
                check(f"[{name}] changed file is not shown as verified",
                      not s["docstatus"].startswith("Verified") and "changed since it was imported" in s["docstatus"]
                      and "edited after import" not in s["doc"], s["docstatus"])
            else:
                check(f"[{name}] document text shown is the original file text",
                      s["docstatus"].startswith("Verified") and s["doc"] == expected_text(name),
                      s["docstatus"])
            check(f"[{name}] summary labelled as Workbench-generated",
                  "generated by the Workbench, not part of the document" in s["summaryLabel"])
            if previous:
                identical = (docs / name).read_bytes() == (docs / previous["path"]).read_bytes()
                check(f"[{name}] no carry-over of previous text", identical or previous["doc"] != s["doc"])
                check(f"[{name}] note box is empty for the new document", s["note"] == "", repr(s["note"]))
                check(f"[{name}] research question belongs to this document",
                      Path(name).stem in s["question"], s["question"][:80])
            if name == tamper:
                page.fill("#note", "trying to decide on a changed file")
                blocked = state(page)
                check(f"[{name}] decisions are disabled for a changed file, with an explanation",
                      not any(blocked["enabled"]) and "cannot be recorded" in blocked["docstatus"] + blocked["error"],
                      json.dumps({k: blocked[k] for k in ("docstatus", "error", "enabled")}))
                (docs / name).write_bytes((docs / name).read_bytes().replace(b"\nedited after import\n", b""))
                check(f"[{name}] a 'Check the file again' button is offered", page.is_visible("#recheck"))
                page.click("#recheck")
                page.wait_for_function("() => document.getElementById('docstatus').textContent.startsWith('Verified')", timeout=10_000)
                page.fill("#note", "")
                s = state(page)
                check(f"[{name}] restored file is verified again and can be decided",
                      s["docstatus"].startswith("Verified") and s["doc"] == expected_text(name), s["docstatus"])
            check(f"[{name}] buttons disabled with an empty note", not any(state(page)["enabled"]))
            page.fill("#note", "   ")
            check(f"[{name}] buttons disabled with a blank note", not any(state(page)["enabled"]))
            note = f"Browser test note for {name}"
            page.fill("#note", note)
            check(f"[{name}] buttons enabled after writing a note", all(state(page)["enabled"]))
            choice = ["APPROVE", "RESEARCH", "QUARANTINE", "REJECT"][index]
            page.click(f"button[data-choice='{choice}']")
            try:
                page.wait_for_function(
                    "prev => document.getElementById('path').textContent !== prev",
                    arg=name, timeout=10_000)
            except Exception:
                print("STUCK after clicking", choice, "on", name, json.dumps(state(page))[:600], console_errors)
                for c in checks: print(("PASS " if c[1] else "FAIL ") + c[0], c[2] if not c[1] else "")
                raise SystemExit(2)
            after = state(page)
            check(f"[{name}] confirmation names the saved document", f"Saved: {choice} for {name}" in after["saved"],
                  after["saved"])
            decided.append((name, choice, note))
            previous = s
            if index < 3:
                s = wait_loaded(page, previous_path=name)
                # the confirmation is about the previous file; it must clear once the reviewer
                # starts on the new one
                page.fill("#note", "x")
                check(f"[after {name}] confirmation clears when work on the next document starts",
                      state(page)["saved"] == "", state(page)["saved"])
                page.fill("#note", "")
                s = state(page)
                s["saved"] = ""  # already checked above

        page.wait_for_function("() => document.getElementById('progress').textContent === 'Review complete'")
        done = state(page)
        check("finished state clears document text and disables controls",
              done["doc"] == "" and not any(done["enabled"]))
        check("no JavaScript errors", not console_errors, "; ".join(console_errors))
        page.reload()
        page.wait_for_function("() => document.getElementById('progress').textContent.length > 0", timeout=10_000)
        probe = state(page)
        check("reloading the page keeps review access", probe["error"] == "" and
              page.evaluate("() => document.getElementById('progress').textContent") == "Review complete", probe["error"])
        browser.close()

    server.shutdown()
    database = sqlite3.connect(db)
    rows = database.execute(
        "SELECT r.relative_path,d.decision,d.review_note,d.reviewer FROM archive_review_decisions d "
        "JOIN review_sources r ON r.source_id=d.source_id ORDER BY d.rowid").fetchall()
    names = {"APPROVE": "APPROVE", "RESEARCH": "NEEDS_RESEARCH", "QUARANTINE": "QUARANTINE", "REJECT": "REJECT"}
    check("exactly one decision saved per document", len(rows) == 4, str(len(rows)))
    for (name, choice, note), row in zip(decided, rows):
        check(f"[{name}] saved decision matches what was clicked, with the reviewer's own note",
              row == (name, names[choice], note, "Browser Test Reviewer"), str(row))
    database.close()

    return checks
