"""Proof views tests (Sprint 6): the metrics, what-was-sent, egress,
redaction and latency endpoints, and the outbound call log.

The API tests run against a live uvicorn subprocess with a temp data
dir, the same pattern as the other API tests.
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from server.proof import clear, egress_audit, entries, record  # noqa: E402


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestOutboundLog(unittest.TestCase):
    def setUp(self):
        clear()

    def test_record_and_entries_newest_first(self):
        record("https://example.test/v1", "model-a", "hello", 12.0, True)
        record("https://example.test/v1", "model-b", "world", 9.0, True)
        rows = entries()
        self.assertEqual([r["model"] for r in rows], ["model-b", "model-a"])
        self.assertEqual(rows[0]["tokens_est"], 1)  # len("world") // 4

    def test_egress_audit_finds_a_leaked_email(self):
        record("https://example.test/v1", "m", "contact test.user@example.com", 1.0, True)
        audit = egress_audit()
        self.assertFalse(audit["passed"])
        self.assertEqual(audit["hit_count"], 1)
        self.assertEqual(audit["hits"][0]["type"], "email")
        # The audit never serves the secret back.
        self.assertNotIn("value", audit["hits"][0])

    def test_egress_audit_catches_a_secret_past_the_stored_prefix(self):
        # The stored prompt is capped at 4000 chars; the audit scans
        # the FULL prompt at record time, so a secret at char 5000
        # is still caught.
        long_prompt = "x" * 5000 + " test.user@example.com"
        record("https://example.test/v1", "m", long_prompt, 1.0, True)
        audit = egress_audit()
        self.assertFalse(audit["passed"])
        self.assertEqual(audit["hit_count"], 1)
        self.assertEqual(audit["hits"][0]["type"], "email")

    def test_egress_audit_passes_on_clean_prompts(self):
        record("https://example.test/v1", "m", "click [EMAIL_1] field", 1.0, True)
        audit = egress_audit()
        self.assertTrue(audit["passed"])
        self.assertEqual(audit["hit_count"], 0)


class TestProofApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # The metrics endpoint reads the latest benchmark-*.json from
        # the repo's results dir — but that dir is gitignored, so a
        # fresh clone has no benchmark file and the endpoint returns
        # metrics: null. Seed a minimal fixture here (and remove it
        # afterwards) so the gate is reproducible on any machine.
        cls.results_dir = REPO_ROOT / "evals" / "sih-benchmark" / "results"
        cls.results_dir.mkdir(parents=True, exist_ok=True)
        # "zz" sorts after any timestamped benchmark file, so the
        # endpoint reads this seed even when real runs exist.
        cls.seed_path = cls.results_dir / "benchmark-zz-test-seed.json"
        cls.seed_path.write_text(
            json.dumps(
                {
                    "benchmark": "eclipse-sih",
                    "sprint": 6,
                    "generated_at": "2026-11-21T00:00:00",
                    "fixtures": 12,
                    "elapsed_s": 0.6,
                    "metrics": {
                        "visual_grounding_first_try": 1.0,
                        "pii_recall": 1.0,
                        "pii_precision": 1.0,
                        "redaction_recoverable_tokens": 0,
                        "resource_mb": 26.7,
                        "latency_p50_s": 0.001,
                    },
                    "detail": {
                        "pii": {"recall": 1.0, "precision": 1.0, "worst_fixture": None, "worst_recall": 1.0, "per_fixture": {}},
                        "redaction": {"recoverable_tokens": 0, "passed": True, "per_fixture": {}},
                        "loop": {
                            "grounding_first_try": 1.0,
                            "grounded_steps": 18,
                            "total_steps": 20,
                            "human_only_handoffs": 2,
                            "step_time_s": {"p50": 0.001, "max": 0.559, "mean": 0.029},
                            "latency_target_s": 6,
                            "p50_within_target": True,
                            "worst_fixture": "application-form",
                            "worst_fixture_s": 0.566,
                            "per_fixture": {},
                            "planner": "mock (ground-truth actions) — NOT live gateway latency",
                        },
                    },
                    "honesty": "test seed",
                }
            ),
            encoding="utf-8",
        )
        cls.tmp = tempfile.mkdtemp(prefix="eclipse-proof-")
        cls.port = _free_port()
        env = dict(os.environ)
        env["ECLIPSE_DATA_DIR"] = cls.tmp
        env["ECLIPSE_TOKEN"] = "test-token-proof"
        env.pop("ANTSEED_API_KEY", None)
        env.pop("GROQ_API_KEY", None)
        env["OLLAMA_BASE_URL"] = "http://127.0.0.1:1/v1"
        cls.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "server.main:app",
             "--host", "127.0.0.1", "--port", str(cls.port)],
            cwd=str(REPO_ROOT), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                code, _ = cls._request("GET", "/health")
                if code == 200:
                    break
            except Exception:
                time.sleep(0.2)
        else:
            raise RuntimeError("proof test server did not start")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(timeout=10)
        cls.seed_path.unlink(missing_ok=True)

    @classmethod
    def _request(cls, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"http://127.0.0.1:{cls.port}{path}", data=data, method=method
        )
        req.add_header("X-Eclipse-Token", "test-token-proof")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()

    def test_endpoints_require_a_token(self):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/proof/metrics")
        try:
            urllib.request.urlopen(req, timeout=10)
            self.fail("expected 401")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 401)

    def test_metrics_come_from_the_latest_benchmark(self):
        code, body = self._request("GET", "/proof/metrics")
        self.assertEqual(code, 200)
        doc = json.loads(body)
        # The seed fixture written in setUpClass (the real benchmark
        # files are gitignored, so the test cannot rely on them).
        self.assertIsNotNone(doc["metrics"])
        self.assertEqual(doc["fixtures"], 12)
        self.assertEqual(doc["metrics"]["redaction_recoverable_tokens"], 0)

    def test_egress_and_redaction_and_latency(self):
        code, body = self._request("GET", "/proof/egress")
        self.assertEqual(code, 200)
        self.assertIn("passed", json.loads(body))
        code, body = self._request("GET", "/proof/redaction")
        self.assertEqual(code, 200)
        doc = json.loads(body)
        self.assertIn("passed", doc)
        self.assertEqual(doc["recoverable_tokens"], 0)
        code, body = self._request("GET", "/proof/latency")
        self.assertEqual(code, 200)
        doc = json.loads(body)
        self.assertIn("p50_within_target", doc)
        self.assertTrue(doc["p50_within_target"])

    def test_what_was_sent_empty_at_start(self):
        code, body = self._request("GET", "/proof/what-was-sent")
        self.assertEqual(code, 200)
        doc = json.loads(body)
        self.assertEqual(doc["count"], 0)
        self.assertEqual(doc["entries"], [])


if __name__ == "__main__":
    unittest.main()
