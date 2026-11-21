"""Test the SIH benchmark harness (Sprint 1).

Runs run.py once into a temp dir and checks: exit 0, 12 fixtures,
all structural checks pass, and the 5 SIH metrics are recorded as
not measured (never invented).
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RUN_PY = REPO_ROOT / "evals" / "sih-benchmark" / "run.py"


class TestHarness(unittest.TestCase):
    def test_run_once_all_fixtures_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "results"
            proc = subprocess.run(
                [sys.executable, str(RUN_PY), "--runs", "1",
                 "--out", str(out_dir)],
                capture_output=True, text=True, timeout=120,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

            files = sorted(out_dir.glob("results-*.json"))
            self.assertEqual(len(files), 1)
            with open(files[0], encoding="utf-8") as f:
                results = json.load(f)

            self.assertEqual(len(results["fixtures"]), 12)
            for name, check in results["fixtures"].items():
                self.assertTrue(check["passed"], f"fixture failed: {name}")
            rate = results["fixture_pass_rate"]
            self.assertEqual((rate["passed"], rate["total"]), (12, 12))

            # The 5 SIH metrics must be null in Sprint 1, with a reason.
            self.assertEqual(len(results["metrics"]), 5)
            for name, metric in results["metrics"].items():
                self.assertIsNone(metric["value"], f"metric invented: {name}")
                self.assertEqual(metric["status"], "not_measured")


if __name__ == "__main__":
    unittest.main()
