"""Consumers of the archive decision ledger agree on each source's latest decision, and a
restored oversized file is reported as excluded again.

Found by independent integration review (2026-11-20):
- `export_staging_manifest` listed every APPROVE ever recorded, so a source later decided
  QUARANTINE still appeared as a proposed copy while `archive-batch-status` counted it
  as quarantined;
- an oversized file restored with its original size and timestamp after a smaller version
  had been admitted produced the same exclusion id, the new sighting was ignored, and
  `archive-batch-status` showed no exclusion although the import output listed it.
"""
import csv
import json
import os
import shutil
from pathlib import Path

from sovereign_workbench.cli import main
from sovereign_workbench.intake import scan_files
from sovereign_workbench.review_batches import (admit, connect, counts, create_next, export_review_csv,
                                                export_staging_manifest, import_review_csv,
                                                register_intake_root)


def _decide(database, batch_id, tmp_path, name, decision, note="Human reviewed"):
    path = tmp_path / f"{name}.csv"
    export_review_csv(database, batch_id, path)
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream); fields = reader.fieldnames; rows = list(reader)
    for row in rows:
        row.update({"decision": decision, "topic": "Other", "maturity": "Research",
                    "authority": "No authority", "confidence": "LOW", "privacy_status": "Reviewed",
                    "canonical_status": "UNRESOLVED", "review_note": note,
                    "reviewer": "Reviewer", "research_question": ""})
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields); writer.writeheader(); writer.writerows(rows)
    assert import_review_csv(database, path) == len(rows)


def test_staging_manifest_uses_only_the_latest_decision(tmp_path: Path):
    source = tmp_path / "source"; source.mkdir()
    (source / "a.txt").write_text("alpha", encoding="utf-8")
    with connect(tmp_path / "state.db") as database:
        register_intake_root(database, source, purpose="t", allowed_suffixes={".txt"})
        admit(database, source, scan_files(source))
        batch = create_next(database, limit=5)
        _decide(database, batch["batch_id"], tmp_path, "first", "APPROVE")
        _decide(database, batch["batch_id"], tmp_path, "second", "QUARANTINE")
        assert counts(database)["quarantined"] == 1
        manifest = export_staging_manifest(database, tmp_path / "manifest.json")
        assert manifest["items"] == []
        _decide(database, batch["batch_id"], tmp_path, "third", "APPROVE", "Re-reviewed and approved")
        manifest = export_staging_manifest(database, tmp_path / "manifest2.json")
        assert len(manifest["items"]) == 1


def test_restored_oversized_file_is_excluded_again(tmp_path: Path, capsys):
    docs = tmp_path / "docs"; docs.mkdir()
    big = docs / "big.txt"
    big.write_bytes(b"x" * (2 * 1024 * 1024))
    original = tmp_path / "big-original.txt"
    shutil.copy2(big, original)
    db = tmp_path / "a.db"
    assert main(["archive-intake-register", str(docs), "--state-db", str(db), "--purpose", "t",
                 "--include", ".txt"]) == 0

    def run():
        assert main(["archive-batch-create", str(docs), "--state-db", str(db), "--include", ".txt",
                     "--max-file-mb", "1"]) == 0
        capsys.readouterr()
        assert main(["archive-batch-status", "--state-db", str(db)]) == 0
        return json.loads(capsys.readouterr().out)["excluded"]["count"]

    capsys.readouterr()
    assert run() == 1
    big.write_text("now small", encoding="utf-8")  # a small version is admitted
    assert run() == 0
    os.remove(big); shutil.copy2(original, big)  # the original returns: same size and mtime
    assert run() == 1
