"""Run the end-to-end synthetic archive workflow at a small size on every CI platform.

The same script runs at full size (4,100 files) by hand:
    python scripts/e2e_synthetic_archive.py --files 4100 --workdir <dir>
"""
import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "e2e_synthetic_archive.py"


def test_end_to_end_workflow_on_a_small_synthetic_archive(tmp_path):
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--files", "150", "--workdir", str(tmp_path / "e2e")],
        capture_output=True, timeout=900)
    try:
        report = json.loads(completed.stdout.decode("utf-8"))
    except ValueError:
        raise AssertionError(f"exit {completed.returncode}; stderr:\n"
                             + completed.stderr.decode("utf-8", "replace")[-4000:]) from None
    failed = [check for check in report["checks"] if not check["ok"]]
    assert completed.returncode == 0 and not failed, json.dumps(failed, ensure_ascii=False, indent=1)
    assert report["checks_passed"] >= 20
