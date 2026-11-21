"""Tests for the Sprint 6 RAG (sqlite-vec + all-MiniLM-L6-v2).

Local only: the embeddings run on the server via ONNX Runtime,
no cloud, no API key. The model files live in
server/rag/models/ (all-MiniLM-L6-v2.onnx + tokenizer.json).
"""
from __future__ import annotations

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

from server.core import config, db
from server.rag import embed as embed_mod
from server.rag import store as rag_store

REPO_ROOT = Path(__file__).parent.parent


class TestEmbeddings(unittest.TestCase):
    def test_embedding_is_384_dim_and_normalised(self):
        vec = embed_mod.embed("Software Development Intern at Acme Tech")
        self.assertEqual(vec.shape, (384,))
        import numpy as np

        self.assertAlmostEqual(float(np.linalg.norm(vec)), 1.0, places=5)

    def test_similar_texts_score_higher_than_unrelated(self):
        a = embed_mod.embed("Software Development Intern at Acme Tech, stipend 15000, Remote")
        b = embed_mod.embed("SDE intern role, remote, 15000 per month")
        c = embed_mod.embed("The weather is sunny today in Hyderabad")
        self.assertGreater(float(a @ b), float(a @ c))
        # The similar pair is above the 0.6 relevance threshold;
        # the unrelated pair is far below it.
        self.assertGreater(float(a @ b), 0.6)
        self.assertLess(float(a @ c), 0.2)

    def test_model_is_available(self):
        self.assertTrue(embed_mod.model_available())


class TestRagStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old = config.DATA_DIR
        config.DATA_DIR = Path(self.tmp.name)
        db.migrate()
        self.conn = db.connect()

    def tearDown(self):
        self.conn.close()
        config.DATA_DIR = self._old
        self.tmp.cleanup()

    def test_index_and_query_returns_the_relevant_chunk(self):
        self.assertTrue(rag_store.index_chunk(
            self.conn, "fact-sde",
            "Software Development Intern at Acme Tech, stipend 15000 per month, Remote",
            "fact",
        ))
        rag_store.index_chunk(
            self.conn, "fact-design",
            "Web Designer role at DesignCo, stipend 8000, Chennai", "fact",
        )
        results = rag_store.query(
            self.conn, "Software Development Intern at Acme Tech, stipend 15000, Remote",
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], "fact-sde")
        self.assertGreaterEqual(results[0]["score"], 0.6)

    def test_unrelated_query_returns_no_weak_match(self):
        rag_store.index_chunk(
            self.conn, "fact-sde",
            "Software Development Intern at Acme Tech, stipend 15000 per month, Remote",
            "fact",
        )
        results = rag_store.query(self.conn, "What is the weather in Hyderabad today")
        self.assertEqual(results, [])

    def test_pii_text_is_refused(self):
        # Redacted text only: a raw email still matches a PII
        # pattern, so the index refuses it.
        self.assertFalse(rag_store.index_chunk(
            self.conn, "fact-pii",
            "Contact test.user@example.com for the role", "fact",
        ))
        self.assertEqual(rag_store.count(self.conn), 0)

    def test_label_based_pii_is_not_caught_by_the_regex_refusal(self):
        # Documented boundary (Sprint 6 review): the refusal is a
        # regex scan. Names, addresses, CVV and passwords are not
        # regex-detectable, so they index — the caller must redact
        # first; the store is a backstop, not a guarantee.
        self.assertTrue(rag_store.index_chunk(self.conn, "n", "My name is Aarav Test", "fact"))
        self.assertTrue(rag_store.index_chunk(self.conn, "c", "My CVV is 123", "fact"))
        self.assertEqual(rag_store.count(self.conn), 2)

    def test_reindexing_the_same_id_overwrites(self):
        rag_store.index_chunk(self.conn, "fact-1", "SDE intern at Acme Tech", "fact")
        rag_store.index_chunk(self.conn, "fact-1", "Backend intern at BetaSoft", "fact")
        self.assertEqual(rag_store.count(self.conn), 1)
        results = rag_store.query(self.conn, "Backend intern at BetaSoft")
        self.assertEqual(len(results), 1)
        self.assertIn("BetaSoft", results[0]["text"])

    def test_forget_excludes_the_chunk(self):
        rag_store.index_chunk(self.conn, "fact-1", "SDE intern at Acme Tech", "fact")
        self.assertTrue(rag_store.forget(self.conn, "fact-1"))
        self.assertEqual(rag_store.count(self.conn), 0)
        self.assertFalse(rag_store.forget(self.conn, "fact-1"))
        self.assertEqual(
            rag_store.query(self.conn, "SDE intern at Acme Tech"), []
        )


# --- API tests (live server) ---

_RAG_TOKEN = "test-token-rag"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _request(base_url, method, path, token=_RAG_TOKEN, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{base_url}{path}", data=data, method=method)
    if token is not None:
        req.add_header("X-Eclipse-Token", token)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())
    except urllib.error.URLError:
        return 0, {}


class TestRagApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.port = _free_port()
        cls.base = f"http://127.0.0.1:{cls.port}"
        env = dict(os.environ)
        env["ECLIPSE_DATA_DIR"] = cls.tmp.name
        env["ECLIPSE_TOKEN"] = _RAG_TOKEN
        env.pop("ANTSEED_API_KEY", None)
        env.pop("GROQ_API_KEY", None)
        env["OLLAMA_BASE_URL"] = "http://127.0.0.1:1/v1"
        venv_python = REPO_ROOT / ".venv" / "bin" / "python"
        python = str(venv_python) if venv_python.exists() else sys.executable
        cls.proc = subprocess.Popen(
            [python, "-m", "uvicorn", "server.main:app",
             "--host", "127.0.0.1", "--port", str(cls.port)],
            cwd=str(REPO_ROOT), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + 20
        while time.time() < deadline:
            status, _ = _request(cls.base, "GET", "/health")
            if status == 200:
                break
            time.sleep(0.3)
        else:
            raise RuntimeError("server did not come up for RAG API test")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(timeout=10)
        cls.tmp.cleanup()

    def test_index_query_forget_flow(self):
        status, body = _request(self.base, "GET", "/rag/status")
        self.assertEqual(status, 200, body)
        self.assertTrue(body["model_available"])
        self.assertEqual(body["chunks"], 0)
        status, body = _request(
            self.base, "POST", "/rag/index",
            body={"id": "fact-sde",
                  "text": "Software Development Intern at Acme Tech, stipend 15000 per month, Remote"},
        )
        self.assertEqual(status, 200, body)
        status, body = _request(
            self.base, "GET",
            "/rag/query?q=Software%20Development%20Intern%20at%20Acme%20Tech%2C%20stipend%2015000%2C%20Remote",
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(len(body["results"]), 1)
        self.assertEqual(body["results"][0]["id"], "fact-sde")
        # PII is refused with a 409.
        status, body = _request(
            self.base, "POST", "/rag/index",
            body={"id": "fact-pii", "text": "Contact test.user@example.com"},
        )
        self.assertEqual(status, 409, body)
        status, body = _request(self.base, "DELETE", "/rag/fact-sde")
        self.assertEqual(status, 200, body)
        status, body = _request(self.base, "GET", "/rag/status")
        self.assertEqual(body["chunks"], 0)

    def test_rag_endpoints_require_token(self):
        status, _ = _request(self.base, "GET", "/rag/status", token=None)
        self.assertEqual(status, 401)


if __name__ == "__main__":
    unittest.main()
