"""Real-browser regression test of the review page (skipped where no browser is available)."""
import os
import shutil
import zipfile

import pytest

pytest.importorskip("playwright.sync_api")
CHROME = os.environ.get("SKW_TEST_CHROME") or shutil.which("google-chrome") or shutil.which("chromium")
pytestmark = pytest.mark.skipif(not CHROME, reason="no Chrome/Chromium executable for browser tests")


def _synthetic_docs(folder):
    folder.mkdir()
    (folder / "soil-inoculant-notes.md").write_bytes(
        b"# Soil inoculant trial notes (SYNTHETIC)\n\nMycorrhizal fungi improved seedling survival.\n"
        b"Citation needed for the baseline figure.\n")
    log = b"Propagation log (SYNTHETIC)\n\nGrevillea cuttings rooted in six weeks under mist.\n"
    (folder / "propagation-log.txt").write_bytes(log)
    (folder / "propagation-log-copy.txt").write_bytes(log)
    body = ('<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
            "<w:p><w:r><w:t>Nursery watering procedure (SYNTHETIC)</w:t></w:r></w:p>"
            "<w:p><w:r><w:t>Water tubestock each morning.</w:t></w:r></w:p></w:body></w:document>")
    with zipfile.ZipFile(folder / "nursery-watering-procedure.docx", "w") as archive:
        archive.writestr("word/document.xml", body)
    return folder


def test_review_page_walkthrough_in_a_real_browser(tmp_path, capsys):
    from review_page_flow import run_review_flow

    checks = run_review_flow(_synthetic_docs(tmp_path / "docs"), CHROME)
    capsys.readouterr()
    failed = [f"{name}: {detail}" for name, ok, detail in checks if not ok]
    assert len(checks) >= 45 and not failed, failed
