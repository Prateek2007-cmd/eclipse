"""Keyword memory tests (Sprint 2).

Unit: store_fact / recall scoring on a temp SQLite file.
API: POST /memory/facts and GET /memory/facts?query=... against a
live server subprocess (same pattern as test_server_smoke.py).
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

from server.core import db  # noqa: E402
from server.memory import keyword  # noqa: E402

TOKEN = "test-token-memory"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _request(base_url, method, path, token=TOKEN, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{base_url}{path}", data=data, method=method)
    if token is not None:
        req.add_header("X-Eclipse-Token", token)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())
    except urllib.error.URLError:
        return 0, {}


class TestKeywordUnit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        db.migrate(self.db_path)
        self.conn = db.connect(self.db_path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_store_and_recall_by_keyword(self):
        keyword.store_fact(self.conn, "User prefers dark mode for dashboards", topic="prefs")
        keyword.store_fact(self.conn, "The gateway host is akash-antseed.duckdns.org", topic="infra")
        keyword.store_fact(self.conn, "User likes filter coffee in the morning", topic="prefs")
        hits = keyword.recall(self.conn, "dark mode dashboards")
        self.assertEqual(len(hits), 1)
        self.assertIn("dark mode", hits[0]["text"])
        self.assertEqual(hits[0]["topic"], "prefs")

    def test_recall_matches_topic_keywords(self):
        keyword.store_fact(self.conn, "Resume is stored locally", topic="resume")
        hits = keyword.recall(self.conn, "where is the resume")
        self.assertEqual(len(hits), 1)

    def test_no_match_returns_empty(self):
        keyword.store_fact(self.conn, "User prefers dark mode", topic="prefs")
        self.assertEqual(keyword.recall(self.conn, "quantum entanglement"), [])

    def test_stopwords_only_query_returns_empty(self):
        keyword.store_fact(self.conn, "User prefers dark mode", topic="prefs")
        self.assertEqual(keyword.recall(self.conn, "what is the"), [])

    def test_limit_is_respected(self):
        for i in range(5):
            keyword.store_fact(self.conn, f"resume fact number {i}", topic="resume")
        hits = keyword.recall(self.conn, "resume", limit=2)
        self.assertEqual(len(hits), 2)


class TestMemoryApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.port = _free_port()
        cls.base = f"http://127.0.0.1:{cls.port}"
        env = dict(os.environ)
        env["ECLIPSE_DATA_DIR"] = cls.tmp.name
        env["ECLIPSE_TOKEN"] = TOKEN
        venv_python = REPO_ROOT / ".venv" / "bin" / "python"
        python = str(venv_python) if venv_python.exists() else sys.executable
        cls.proc = subprocess.Popen(
            [python, "-m", "uvicorn", "server.main:app",
             "--host", "127.0.0.1", "--port", str(cls.port)],
            cwd=str(REPO_ROOT),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + 20
        while time.time() < deadline:
            status, _ = _request(cls.base, "GET", "/health")
            if status == 200:
                break
            time.sleep(0.3)
        else:
            raise RuntimeError("server did not come up for memory API test")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(timeout=10)
        cls.tmp.cleanup()

    def test_post_and_get_fact(self):
        status, body = _request(self.base, "POST", "/memory/facts",
                                body={"text": "User prefers dark mode", "topic": "prefs"})
        self.assertEqual(status, 201, body)
        self.assertTrue(body["id"].startswith("fact-"))
        status, body = _request(self.base, "GET", "/memory/facts?query=dark+mode")
        self.assertEqual(status, 200, body)
        self.assertEqual(len(body["facts"]), 1)
        self.assertIn("dark mode", body["facts"][0]["text"])

    def test_memory_requires_token(self):
        status, _ = _request(self.base, "GET", "/memory/facts?query=x", token=None)
        self.assertEqual(status, 401)

    def test_post_rejects_empty_text(self):
        status, _ = _request(self.base, "POST", "/memory/facts", body={"text": ""})
        self.assertEqual(status, 422)


if __name__ == "__main__":
    unittest.main()
