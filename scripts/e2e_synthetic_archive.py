"""Reproducible end-to-end run of the archive workflow on a synthetic archive.

Generates a deterministic synthetic archive (text, Markdown, DOCX and PDF, with duplicates,
non-UTF-8 and empty files, non-Latin text, nested folders and one oversized file), then
drives the workflow the way an operator does, through the `skw` command line and the local
review screen's HTTP API:

  register -> import in batches (one import is killed part-way and re-run) -> export review
  CSVs -> import decisions -> review screen (decide, skip, restart) -> search -> change,
  delete and rename sources -> search again.

Every step is checked and timed. The script prints a JSON report and exits 1 if any check
fails. Only synthetic data is used; nothing leaves the machine.

    python scripts/e2e_synthetic_archive.py --files 4100 --workdir C:\\temp\\skw-e2e
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import platform
import random
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

WORDS = ("seedling survival plot compost rainfall mulch canopy nursery trial cutting grevillea "
         "banksia irrigation soil nitrogen germination transect record observation summary method "
         "result evidence draft protocol specification hypothesis sample measurement weekly site").split()
NON_LATIN = ("日本語の記録", "种子发芽试验", "Ελληνικά δείγματα", "русский отчёт", "café résumé naïve", "🌱🌿")
ROOT_DIRS = ("projects", "field-notes", "correspondence", "archive/2019", "archive/2020/q3",
             "research/drafts", "Résumés et notes", "handover")


def _prose(rng: random.Random, words: int) -> str:
    lines, line = [], []
    for _ in range(words):
        line.append(rng.choice(WORDS))
        if len(line) >= rng.randint(8, 16):
            lines.append(" ".join(line).capitalize() + ".")
            line = []
    if line:
        lines.append(" ".join(line).capitalize() + ".")
    return "\n".join(lines)


def _docx(text: str) -> bytes:
    body = "".join(f"<w:p><w:r><w:t>{line}</w:t></w:r></w:p>" for line in text.split("\n"))
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml",
                         '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                         '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-'
                         'officedocument.wordprocessingml.document.main+xml"/></Types>')
        archive.writestr("word/document.xml",
                         '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/'
                         f'wordprocessingml/2006/main"><w:body>{body}</w:body></w:document>')
    return stream.getvalue()


def _pdf(text: str) -> bytes:
    # Helvetica in a plain content stream: keep to Latin-1 and drop PDF string delimiters.
    lines = [line.encode("latin-1", "ignore").decode("latin-1").replace("\\", "").replace("(", "").replace(")", "")
             for line in text.split("\n")[:40]]
    content = "BT /F1 10 Tf 40 800 Td 12 TL " + " ".join(f"({line}) '" for line in lines) + " ET"
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R "
        "/Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{offset:010d} 00000 n \n" for offset in offsets).encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def generate(root: Path, count: int, seed: int) -> dict[str, int]:
    """Write `count` synthetic files under `root`; return counts by kind."""
    rng = random.Random(seed)
    kinds: dict[str, int] = {}
    written: list[Path] = []

    def put(relative: str, data: bytes, kind: str) -> None:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        written.append(path)
        kinds[kind] = kinds.get(kind, 0) + 1

    specials = 6
    put("handover/empty.md", b"", "empty")
    put("archive/2019/legacy-bytes.txt", b"\xff\xfe\x00 legacy bytes seedling\n", "non_utf8")
    put("projects/oversized.txt", b"oversized seedling log\n" * (22 * 1024 * 1024 // 23), "oversized")
    put("field-notes/日本語/観察.md", "観察記録 seedling 🌱 survival 82%\n".encode(), "non_latin_name")
    put("correspondence/contact sheet.txt",
        b"Contact: synthetic.person@example.invalid phone 0400 000 000\nseedling order\n", "privacy")
    put("research/drafts/gap.md", b"Rainfall claim. TODO: confirm citation for rainfall data.\n", "research_gap")
    for index in range(count - specials):
        folder = rng.choice(ROOT_DIRS) + ("/" + rng.choice(("a", "b", "c")) if rng.random() < 0.4 else "")
        roll = rng.random()
        if written and roll < 0.04:
            source = rng.choice(written)
            put(f"{folder}/copy-{index:05d}{source.suffix}", source.read_bytes(), "duplicate")
            continue
        text = _prose(rng, rng.randint(80, 2500 if rng.random() < 0.9 else 20000))
        if rng.random() < 0.05:
            text += "\n" + rng.choice(NON_LATIN) + " seedling"
        if roll < 0.70:
            suffix = rng.choice((".md", ".txt"))
            put(f"{folder}/note-{index:05d}{suffix}", text.encode("utf-8"), "text")
        elif roll < 0.88:
            put(f"{folder}/report-{index:05d}.docx", _docx(text), "docx")
        else:
            put(f"{folder}/scan-{index:05d}.pdf", _pdf(text), "pdf")
    return kinds


class Runner:
    def __init__(self, workdir: Path):
        self.workdir = workdir
        self.checks: list[dict[str, object]] = []
        self.timings: dict[str, float] = {}

    def check(self, name: str, ok: bool, detail: object = "") -> bool:
        self.checks.append({"check": name, "ok": bool(ok), "detail": detail})
        return bool(ok)

    def skw(self, *args: str, expect: int = 0, timing: str | None = None) -> dict | None:
        started = time.perf_counter()
        completed = subprocess.run([sys.executable, "-m", "sovereign_workbench.cli", *args],
                                   capture_output=True, cwd=self.workdir)
        if timing:
            self.timings[timing] = round(time.perf_counter() - started, 3)
        if completed.returncode != expect:
            raise RuntimeError(f"skw {args[0]} exited {completed.returncode}: "
                               + completed.stderr.decode("utf-8", "replace")[-2000:])
        out = completed.stdout.decode("utf-8")  # must be valid UTF-8 even on Windows
        return json.loads(out) if out.strip() else None


def _db_invariants(db: Path) -> list[str]:
    problems = []
    with sqlite3.connect(db) as database:
        for batch_id, count in database.execute("SELECT batch_id,item_count FROM review_batches"):
            items = database.execute("SELECT COUNT(*) FROM review_batch_items WHERE batch_id=?",
                                     (batch_id,)).fetchone()[0]
            if items != count:
                problems.append(f"batch {batch_id[:12]} has {items} items, header says {count}")
        missing = database.execute(
            "SELECT COUNT(*) FROM review_batch_items i LEFT JOIN archive_review_candidates c "
            "ON c.source_id=i.source_id WHERE c.source_id IS NULL").fetchone()[0]
        if missing:
            problems.append(f"{missing} batched items have no candidate")
        twice = database.execute("SELECT COUNT(*) FROM (SELECT source_id FROM review_batch_items "
                                 "GROUP BY source_id HAVING COUNT(*)>1)").fetchone()[0]
        if twice:
            problems.append(f"{twice} sources are in more than one batch")
    return problems


def _review_api(db: Path):
    from sovereign_workbench.local_review_web import make_server
    server, token = make_server(db, "Synthetic Reviewer")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"

    def call(method: str, path: str, body: dict | None = None):
        request = urllib.request.Request(base + path, method=method, headers={
            "Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            data=None if body is None else json.dumps(body).encode())
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def stop() -> None:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    return call, stop


def run(files: int, workdir: Path, seed: int, kill_after: float) -> dict[str, object]:
    marker = workdir / ".skw-e2e-workdir"
    if workdir.exists() and any(workdir.iterdir()):
        if not marker.is_file():  # never delete a folder this script did not create
            raise SystemExit(f"Refusing to use {workdir}: it is not empty and was not created by "
                             "this script. Choose a new folder.")
        shutil.rmtree(workdir)
    archive, db = workdir / "archive", workdir / "state.db"
    archive.mkdir(parents=True)
    marker.write_text("synthetic end-to-end work folder; safe to delete\n", encoding="utf-8")
    r = Runner(workdir)
    include = ["--include", ".md", ".txt", ".docx", ".pdf"]

    started = time.perf_counter()
    kinds = generate(archive, files, seed)
    r.timings["generate_archive"] = round(time.perf_counter() - started, 3)
    total_bytes = sum(p.stat().st_size for p in archive.rglob("*") if p.is_file())

    r.skw("archive-intake-register", str(archive), "--state-db", str(db), "--purpose", "synthetic e2e", *include)

    # Restart recovery: kill the first import part-way, then require a consistent database.
    process = subprocess.Popen([sys.executable, "-m", "sovereign_workbench.cli", "archive-batch-create",
                                str(archive), "--state-db", str(db), *include, "--limit", "1000"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=workdir)
    # Kill once sources are admitted (committed) but before the batch itself is committed.
    deadline = time.monotonic() + 1800
    while process.poll() is None and time.monotonic() < deadline:
        try:
            with sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=0.1) as probe:
                if probe.execute("SELECT COUNT(*) FROM review_sources").fetchone()[0]:
                    break
        except sqlite3.Error:
            pass
        time.sleep(0.05)
    time.sleep(kill_after)
    killed = process.poll() is None
    process.kill()
    process.wait()
    r.check("killed import leaves a consistent database", not _db_invariants(db), _db_invariants(db))
    r.check("import was still running when killed", killed, f"{kill_after}s after sources were admitted")
    with sqlite3.connect(db) as probe:
        r.check("killed import committed no batch",
                probe.execute("SELECT COUNT(*) FROM review_batches").fetchone()[0] == 0, "")

    batches, runs = [], 0
    while True:
        runs += 1
        outcome = r.skw("archive-batch-create", str(archive), "--state-db", str(db), *include,
                        "--limit", "1000", timing=f"batch_create_run_{runs}")
        if not outcome["batch"]:
            break
        batches.append(outcome["batch"])
        if runs > 20:
            break
    overview = r.skw("archive-batch-status", "--state-db", str(db))
    status, excluded = overview["status"], overview.get("excluded", {"count": 0, "files": []})
    batched = sum(len(b["items"]) for b in batches)
    r.check("every file is either batched once or recorded as excluded",
            batched + excluded["count"] == files and status["pending"] == 0,
            {"batched": batched, "excluded": excluded["count"], "files": files, "status": status})
    r.check("the oversized file is recorded as excluded with its reason",
            [(f["relative_path"], f["reason"]) for f in excluded["files"]]
            == [("projects/oversized.txt", "OVER_SIZE_LIMIT")], excluded)
    r.check("database invariants after import", not _db_invariants(db), _db_invariants(db))

    # Human review: all but the last batch through CSV, the last through the review screen.
    decisions = {"APPROVE": 0, "QUARANTINE": 0, "NEEDS_RESEARCH": 0, "REJECT": 0}
    started = time.perf_counter()
    for number, batch in enumerate(batches[:-1], start=1):
        exported = workdir / f"review-{number}.csv"
        r.skw("archive-batch-export", batch["batch_id"], "--state-db", str(db), "--output", str(exported))
        with exported.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        for index, row in enumerate(rows):
            decision = ("APPROVE", "QUARANTINE", "NEEDS_RESEARCH", "REJECT")[index % 4]
            decisions[decision] += 1
            row.update(decision=decision, topic="Other", maturity="Research", authority="No authority",
                       confidence="MEDIUM", review_note=f"synthetic decision {index}",
                       reviewer="Synthetic Reviewer",
                       research_question="Which claims need primary evidence?" if decision == "NEEDS_RESEARCH" else "")
        decided = workdir / f"review-{number}-decided.csv"
        with decided.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        imported = r.skw("archive-review-import", str(decided), "--state-db", str(db))["imported"]
        r.check(f"CSV batch {number} imports every decision", imported == len(rows), imported)
    r.timings["csv_review_round_trip"] = round(time.perf_counter() - started, 3)

    # Review screen on the last batch: make its first two files undecidable, then work through.
    last_items = [item for item in batches[-1]["items"]]
    gone = archive / last_items[0]["relative_path"]
    gone.rename(gone.with_name(gone.name + ".moved"))
    call, stop = _review_api(db)
    screen_decided = screen_skipped = 0
    started = time.perf_counter()
    try:
        for _ in range(len(last_items) + 5):
            status_code, reply = call("GET", "/api/next")
            item = reply.get("item")
            if not item:
                break
            source = call("GET", f"/api/source?source_id={item['source_id']}")[1]
            if source.get("hash_verified"):
                code, _ = call("POST", "/api/decision", {"source_id": item["source_id"], "decision": "APPROVE",
                                                        "note": "checked on screen", "research_question": ""})
                screen_decided += code == 200
            else:
                code, _ = call("POST", "/api/skip", {"source_id": item["source_id"]})
                screen_skipped += code == 200
        final = call("GET", "/api/next")[1]
    finally:
        stop()
    r.timings["review_screen_pass"] = round(time.perf_counter() - started, 3)
    r.check("review screen reaches the end of the queue", final["item"] is None and final["remaining"] == 0, final)
    r.check("moved file is skipped, not decided", screen_skipped >= 1 and final["skipped"] == screen_skipped,
            {"skipped": screen_skipped})
    call, stop = _review_api(db)  # restart: skipped files come back, nothing was recorded for them
    try:
        again = call("GET", "/api/next")[1]
    finally:
        stop()
    r.check("skipped files return after restart", again["item"] is not None and again["skipped"] == 0
            and again["remaining"] == screen_skipped, {"remaining": again["remaining"]})
    gone.with_name(gone.name + ".moved").rename(gone)

    status = r.skw("archive-batch-status", "--state-db", str(db))["status"]
    r.check("reviewed + quarantined counts match decisions",
            status["quarantined"] == decisions["QUARANTINE"]
            and status["reviewed"] == batched - decisions["QUARANTINE"] - screen_skipped,
            {"status": status, "decisions": decisions, "screen_decided": screen_decided})

    # Search: verified hits, non-Latin text through a pipe, complete unverified lists.
    for label, query in (("common", "seedling"), ("rare", "transect weekly"), ("non_latin", "日本語"),
                         ("privacy", "example.invalid")):
        found = r.skw("archive-search", query, "--state-db", str(db), "--limit", "500", timing=f"search_{label}")
        r.check(f"search '{label}' finds results", found["matching_sources"] > 0, found["matching_sources"])
        r.check(f"search '{label}' hits are all verified",
                all(hit["verification"] == "VERIFIED" for hit in found["results"]), "")

    sources = sorted(p for p in archive.rglob("*") if p.is_file() and p.suffix in (".md", ".txt")
                     and p.stat().st_size < 1_000_000 and b"seedling" in p.read_bytes())[:15]
    for path in sources[:5]:
        path.write_bytes(path.read_bytes() + b"\nedited after import seedling\n")
    for path in sources[5:10]:
        path.unlink()
    for path in sources[10:15]:
        path.rename(path.with_name("renamed-" + path.name))
    found = r.skw("archive-search", "seedling", "--state-db", str(db), "--limit", "500", timing="search_after_changes")
    unverified = {u["relative_path"]: u["status"] for u in found["unverified_sources"]}
    changed = [p.relative_to(archive).as_posix() for p in sources[:5]]
    missing = [p.relative_to(archive).as_posix() for p in sources[5:15]]
    r.check("edited files are reported as changed", all(unverified.get(p) == "CHANGED_SINCE_INTAKE" for p in changed),
            {p: unverified.get(p) for p in changed})
    r.check("deleted and renamed files are reported missing", all(unverified.get(p) == "MISSING" for p in missing),
            {p: unverified.get(p) for p in missing})
    shown = {hit["relative_path"] for hit in found["results"] if hit["content_matches"]}
    r.check("no text is shown from changed files", not shown & set(changed), sorted(shown & set(changed)))

    return {
        "contract": "sovereign.workbench.e2e-synthetic-archive.v1",
        "platform": {"system": platform.system(), "release": platform.release(),
                     "python": platform.python_version()},
        "archive": {"files": files, "bytes": total_bytes, "kinds": kinds, "seed": seed},
        "batches": len(batches), "batch_create_runs": runs,
        "timings_seconds": r.timings,
        "checks_passed": sum(c["ok"] for c in r.checks), "checks_failed": sum(not c["ok"] for c in r.checks),
        "checks": r.checks,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--files", type=int, default=4100)
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=20261120)
    parser.add_argument("--kill-after", type=float, default=0.0,
                        help="seconds after sources are admitted before the first import is killed")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    if args.files < 20:
        parser.error("--files must be at least 20")
    report = run(args.files, args.workdir.resolve(), args.seed, args.kill_after)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        args.report.write_text(text, encoding="utf-8")
    sys.stdout.buffer.write(text.encode("utf-8") + b"\n")
    return 0 if report["checks_failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
