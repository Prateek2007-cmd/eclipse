"""Sprint 5 tests: the vault (Fernet file, per-task grants, fill),
the fill map in the loop, and cold mail.

Run: .venv/bin/python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import os
import stat
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from server.core import config, db
from server.jobs import profile
from server.tasks import loop
from server.vault import fill as fill_mod
from server.vault import grants as grant_store
from server.vault.store import Vault, VaultError

PASSWORD = "correct horse battery staple"
WRONG_PASSWORD = "wrong password"


class TestVaultFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Vault(data_dir=Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_unlock_creates_the_file_with_0600(self):
        self.vault.create(PASSWORD)
        path = Path(self.tmp.name) / "vault.fernet"
        self.assertTrue(path.exists())
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertTrue(self.vault.unlocked)

    def test_unlock_without_a_vault_fails(self):
        # Create and unlock are separate: a first unlock must not
        # silently create the vault with a mistyped password.
        with self.assertRaises(VaultError):
            self.vault.unlock(PASSWORD)

    def test_create_twice_fails(self):
        self.vault.create(PASSWORD)
        with self.assertRaises(VaultError):
            self.vault.create(PASSWORD)

    def test_wrong_password_is_refused(self):
        self.vault.create(PASSWORD)
        self.vault.lock()
        with self.assertRaises(VaultError):
            self.vault.unlock(WRONG_PASSWORD)

    def test_store_and_read_an_opted_in_password(self):
        self.vault.create(PASSWORD)
        self.vault.store_password("https://example.com", "password", "s3cr3t")
        self.assertEqual(
            self.vault.get_password("https://example.com", "password"), "s3cr3t"
        )
        # A different origin does not see it.
        self.assertIsNone(self.vault.get_password("https://other.com", "password"))

    def test_locked_vault_refuses_reads_and_writes(self):
        self.vault.create(PASSWORD)
        self.vault.store_password("https://example.com", "password", "s3cr3t")
        self.vault.lock()
        self.assertFalse(self.vault.unlocked)
        with self.assertRaises(VaultError):
            self.vault.get_password("https://example.com", "password")
        with self.assertRaises(VaultError):
            self.vault.store_password("https://example.com", "password", "x")

    def test_the_file_does_not_contain_the_plaintext(self):
        self.vault.create(PASSWORD)
        self.vault.store_password("https://example.com", "password", "s3cr3t-value")
        raw = (Path(self.tmp.name) / "vault.fernet").read_text()
        self.assertNotIn("s3cr3t-value", raw)
        self.assertNotIn(PASSWORD, raw)

    def test_delete_password(self):
        self.vault.create(PASSWORD)
        self.vault.store_password("https://example.com", "password", "s3cr3t")
        self.assertTrue(self.vault.delete_password("https://example.com", "password"))
        self.assertIsNone(self.vault.get_password("https://example.com", "password"))
        self.assertFalse(self.vault.delete_password("https://example.com", "password"))

    def test_key_idle_locks_after_10_minutes(self):
        self.vault.create(PASSWORD)
        self.assertTrue(self.vault.unlocked)
        # Fake the clock: 11 minutes of idle.
        self.vault._last_used_at = int(time.time()) - 11 * 60
        self.assertFalse(self.vault.unlocked)
        with self.assertRaises(VaultError):
            self.vault.get_password("https://example.com", "password")

    def test_corrupt_vault_file_is_a_vault_error(self):
        path = Path(self.tmp.name) / "vault.fernet"
        path.write_text("not json at all")
        with self.assertRaises(VaultError):
            self.vault.unlock(PASSWORD)


class TestVaultGrants(unittest.TestCase):
    def setUp(self):
        self.store = grant_store.GrantStore()

    def test_grant_expires_after_15_minutes(self):
        grant = self.store.create("task-1")
        self.assertIsNotNone(self.store.get("task-1"))
        # Fake the clock: 16 minutes later.
        grant.expires_at = int(time.time()) - 1
        self.assertIsNone(self.store.get("task-1"))

    def test_idle_lock_after_10_minutes(self):
        grant = self.store.create("task-1")
        grant.last_used_at = int(time.time()) - 11 * 60
        self.assertIsNone(self.store.get("task-1"))

    def test_card_lives_only_in_the_grant_and_is_wiped(self):
        grant = self.store.create(
            "task-1",
            card={"number": "4111 1111 1111 1111", "expiry": "12/28", "cvv": "123"},
        )
        self.assertEqual(grant.card_last4, "1111")
        self.store.revoke_task("task-1")
        self.assertIsNone(grant.card)
        self.assertIsNone(self.store.get("task-1"))

    def test_revoke_all(self):
        self.store.create("task-1")
        self.store.create("task-2")
        self.assertEqual(self.store.revoke_all(), 2)
        self.assertEqual(self.store.status()["grants"], [])


class TestFillMap(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old = config.DATA_DIR
        config.DATA_DIR = Path(self.tmp.name)
        db.migrate()
        self.conn = db.connect()
        self.vault = Vault(data_dir=Path(self.tmp.name))
        profile.store_profile(self.conn, profile.TEST_PROFILE)

    def tearDown(self):
        self.conn.close()
        config.DATA_DIR = self._old
        self.tmp.cleanup()

    def _approval_grant(self, task_id):
        now = int(time.time())
        self.conn.execute(
            "INSERT INTO grants (id, origin, scope, expires_at, created_at)"
            " VALUES (?, ?, 'approve', ?, ?)",
            (uuid.uuid4().hex[:12], task_id, now + 600, now),
        )
        self.conn.commit()
        return [{"id": "g1"}]

    def test_identity_placeholders_come_from_the_profile(self):
        # The job-hunt apply flow (a "role:" constraint) authorises
        # the identity fill (M8: no approval, no role → no fill).
        # Each placeholder is filled for its own element (the
        # binding: the token appears in the element's redacted
        # value or its label matches the token's field).
        constraints = "role: role_1\nurl: http://localhost/jobs/role_1"
        fill, _denied = fill_mod.build_fill_map(
            self.conn, "task-1", ["[NAME_1]"],
            "http://localhost", self.vault, approval_grants=None,
            constraints=constraints,
            target={"id": "el_name", "name": "Full name", "text": "", "value": "[NAME_1]"},
        )
        self.assertEqual(fill["[NAME_1]"], "Test User")
        fill, _denied = fill_mod.build_fill_map(
            self.conn, "task-1", ["[EMAIL_1]"],
            "http://localhost", self.vault, approval_grants=None,
            constraints=constraints,
            target={"id": "el_email", "name": "Email", "text": "", "value": "[EMAIL_1]"},
        )
        self.assertEqual(fill["[EMAIL_1]"], "test.user@example.com")
        fill, _denied = fill_mod.build_fill_map(
            self.conn, "task-1", ["[PHONE_1]"],
            "http://localhost", self.vault, approval_grants=None,
            constraints=constraints,
            target={"id": "el_phone", "name": "Phone", "text": "", "value": "[PHONE_1]"},
        )
        self.assertEqual(fill["[PHONE_1]"], "99999 99999")
        # A token that does not belong to the element is not
        # filled (B4): the email token in the phone field.
        fill, _denied = fill_mod.build_fill_map(
            self.conn, "task-1", ["[EMAIL_1]"],
            "http://localhost", self.vault, approval_grants=None,
            constraints=constraints,
            target={"id": "el_phone", "name": "Phone", "text": "", "value": ""},
        )
        self.assertEqual(fill, {})

    def test_card_needs_the_vault_unlocked_and_an_approval(self):
        # No vault grant at all → no card fill.
        fill, denied = fill_mod.build_fill_map(
            self.conn, "task-1", ["[CARD_NUMBER]", "[CVV]"],
            "http://localhost", self.vault, approval_grants=[{"id": "g1"}],
            target={"id": "el_card_number", "name": "Card number", "text": "", "value": "[CARD_NUMBER]"},
        )
        self.assertEqual(fill, {})
        self.assertIsNotNone(denied)
        # Vault unlocked + a vault grant, but no approval → no fill.
        self.vault.create(PASSWORD)
        grant_store.GRANTS.create(
            "task-1",
            card={"number": "4111 1111 1111 1111", "expiry": "12/28", "cvv": "123"},
        )
        fill, denied = fill_mod.build_fill_map(
            self.conn, "task-1", ["[CARD_NUMBER]", "[CVV]"],
            "http://localhost", self.vault, approval_grants=None,
            target={"id": "el_card_number", "name": "Card number", "text": "", "value": "[CARD_NUMBER]"},
        )
        self.assertEqual(fill, {})
        self.assertIsNotNone(denied)
        # Vault unlocked + a vault grant + an approval → filled.
        approvals = self._approval_grant("task-1")
        fill, denied = fill_mod.build_fill_map(
            self.conn, "task-1", ["[CARD_NUMBER]"],
            "http://localhost", self.vault, approval_grants=approvals,
            target={"id": "el_card_number", "name": "Card number", "text": "", "value": "[CARD_NUMBER]"},
        )
        self.assertIsNone(denied)
        self.assertEqual(fill["[CARD_NUMBER]"], "4111 1111 1111 1111")
        fill, denied = fill_mod.build_fill_map(
            self.conn, "task-1", ["[CVV]"],
            "http://localhost", self.vault, approval_grants=approvals,
            target={"id": "el_card_cvv", "name": "CVV", "text": "", "value": "[CVV]"},
        )
        self.assertIsNone(denied)
        self.assertEqual(fill["[CVV]"], "123")
        # [CARD_LAST4] fills the full card number too (B2: the
        # redactor issues [CARD_LAST4] for a card number).
        fill, denied = fill_mod.build_fill_map(
            self.conn, "task-1", ["[CARD_LAST4]"],
            "http://localhost", self.vault, approval_grants=approvals,
            target={"id": "el_card_number", "name": "Card number", "text": "", "value": "[CARD_LAST4]"},
        )
        self.assertIsNone(denied)
        self.assertEqual(fill["[CARD_LAST4]"], "4111 1111 1111 1111")
        grant_store.GRANTS.revoke_all()

    def test_otp_is_never_filled(self):
        self.vault.create(PASSWORD)
        grant_store.GRANTS.create("task-1")
        approvals = self._approval_grant("task-1")
        fill, _denied = fill_mod.build_fill_map(
            self.conn, "task-1", ["[OTP]"],
            "http://localhost", self.vault, approval_grants=approvals,
            target={"id": "el_otp", "name": "OTP", "text": "", "value": "[OTP]"},
        )
        self.assertEqual(fill, {})
        grant_store.GRANTS.revoke_all()

    def test_stored_password_fills_by_origin(self):
        self.vault.create(PASSWORD)
        grant_store.GRANTS.create("task-1")
        approvals = self._approval_grant("task-1")
        self.vault.store_password("http://localhost", "password", "hunter2")
        fill, denied = fill_mod.build_fill_map(
            self.conn, "task-1", ["[PASSWORD_1]"],
            "http://localhost", self.vault, approval_grants=approvals,
            target={"id": "el_password", "name": "Password", "text": "", "value": "[PASSWORD_1]"},
        )
        self.assertIsNone(denied)
        self.assertEqual(fill["[PASSWORD_1]"], "hunter2")
        # A different origin does not fill it.
        fill, _denied = fill_mod.build_fill_map(
            self.conn, "task-1", ["[PASSWORD_1]"],
            "https://other.com", self.vault, approval_grants=approvals,
            target={"id": "el_password", "name": "Password", "text": "", "value": "[PASSWORD_1]"},
        )
        self.assertEqual(fill, {})
        grant_store.GRANTS.revoke_all()


if __name__ == "__main__":
    unittest.main()


# --- API tests (live server, same pattern as test_tasks.TestTasksApi) ---

import socket as _socket
import subprocess as _subprocess
import sys as _sys
import urllib.error as _urlerror
import urllib.request as _urlrequest

_VAULT_TOKEN = "test-token-vault"
_REPO_ROOT = Path(__file__).parent.parent


def _free_port() -> int:
    with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _request(base_url, method, path, token=_VAULT_TOKEN, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = _urlrequest.Request(f"{base_url}{path}", data=data, method=method)
    if token is not None:
        req.add_header("X-Eclipse-Token", token)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with _urlrequest.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())
    except _urlerror.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())
    except _urlerror.URLError:
        return 0, {}


class TestVaultApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.port = _free_port()
        cls.base = f"http://127.0.0.1:{cls.port}"
        env = dict(os.environ)
        env["ECLIPSE_DATA_DIR"] = cls.tmp.name
        env["ECLIPSE_TOKEN"] = _VAULT_TOKEN
        env.pop("ANTSEED_API_KEY", None)
        env.pop("GROQ_API_KEY", None)
        env["OLLAMA_BASE_URL"] = "http://127.0.0.1:1/v1"
        venv_python = _REPO_ROOT / ".venv" / "bin" / "python"
        python = str(venv_python) if venv_python.exists() else _sys.executable
        cls.proc = _subprocess.Popen(
            [python, "-m", "uvicorn", "server.main:app",
             "--host", "127.0.0.1", "--port", str(cls.port)],
            cwd=str(_REPO_ROOT), env=env,
            stdout=_subprocess.DEVNULL, stderr=_subprocess.DEVNULL,
        )
        import time as _time

        deadline = _time.time() + 20
        while _time.time() < deadline:
            status, _ = _request(cls.base, "GET", "/health")
            if status == 200:
                break
            _time.sleep(0.3)
        else:
            raise RuntimeError("server did not come up for vault API test")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(timeout=10)
        cls.tmp.cleanup()

    def test_a_vault_create_before_unlock(self):
        # Runs first (alphabetical): unlock before create is a 404
        # (create first), and create twice is a 409.
        status, body = _request(
            self.base, "POST", "/tasks",
            body={"goal": "Create the vault"},
        )
        self.assertEqual(status, 201, body)
        task_id = body["id"]
        status, body = _request(
            self.base, "POST", "/vault/unlock",
            body={"password": PASSWORD, "task_id": task_id},
        )
        self.assertEqual(status, 404, body)
        status, body = _request(
            self.base, "POST", "/vault/create",
            body={"password": PASSWORD},
        )
        self.assertEqual(status, 200, body)
        status, body = _request(
            self.base, "POST", "/vault/create",
            body={"password": PASSWORD},
        )
        self.assertEqual(status, 409, body)

    def test_unlock_fill_and_lock(self):
        # Create a task first.
        status, body = _request(
            self.base, "POST", "/tasks",
            body={"goal": "Buy this book with my saved card"},
        )
        self.assertEqual(status, 201, body)
        task_id = body["id"]
        # Lock first: the tests share one server, so an earlier
        # test may have left the key in memory.
        status, body = _request(self.base, "POST", "/vault/lock", body={})
        self.assertEqual(status, 200, body)
        # The vault starts locked (no key, no grants).
        status, body = _request(self.base, "GET", "/vault/status")
        self.assertEqual(status, 200, body)
        self.assertFalse(body["unlocked"])
        self.assertFalse(body["has_grants"])
        # A wrong password is refused.
        status, body = _request(
            self.base, "POST", "/vault/unlock",
            body={"password": "wrong", "task_id": task_id},
        )
        self.assertEqual(status, 401, body)
        # Unlock with a card (per task, never stored).
        status, body = _request(
            self.base, "POST", "/vault/unlock",
            body={"password": PASSWORD, "task_id": task_id,
                  "card_number": "4111 1111 1111 1111",
                  "card_expiry": "12/28", "card_cvv": "123"},
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["card_last4"], "1111")
        # /vault/fill says a card fill is possible — and never
        # returns the value.
        status, body = _request(
            self.base, "POST", "/vault/fill",
            body={"task_id": task_id, "origin": "http://localhost",
                  "field": "card_number"},
        )
        self.assertEqual(status, 200, body)
        self.assertTrue(body["ok"])
        self.assertNotIn("4111", json.dumps(body))
        # The status shows the grant and the card last 4, never
        # the card number.
        status, body = _request(
            self.base, "GET", f"/vault/status?task_id={task_id}",
        )
        self.assertEqual(status, 200, body)
        self.assertTrue(body["unlocked"])
        self.assertTrue(body["has_grants"])
        self.assertEqual(len(body["grants"]), 1)
        self.assertEqual(body["grants"][0]["card_last4"], "1111")
        # Revoke-all, then no grant is live (the key stays in
        # memory until an explicit lock — the two fields mean
        # different things, and the status says so).
        status, body = _request(self.base, "POST", "/vault/revoke-all")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["revoked"], 1)
        status, body = _request(
            self.base, "GET", f"/vault/status?task_id={task_id}",
        )
        self.assertEqual(status, 200, body)
        self.assertFalse(body["has_grants"])
        status, body = _request(
            self.base, "POST", "/vault/lock", body={},
        )
        self.assertEqual(status, 200, body)
        self.assertTrue(body["locked"])
        status, body = _request(
            self.base, "GET", f"/vault/status?task_id={task_id}",
        )
        self.assertEqual(status, 200, body)
        self.assertFalse(body["unlocked"])
        self.assertFalse(body["has_grants"])

    def test_vault_endpoints_require_token(self):
        status, _ = _request(self.base, "GET", "/vault/status", token=None)
        self.assertEqual(status, 401)

    def test_store_password_refuses_a_card_number(self):
        # M3: card numbers and CVVs are never stored, by design.
        status, body = _request(
            self.base, "POST", "/tasks",
            body={"goal": "Log in to the test portal"},
        )
        self.assertEqual(status, 201, body)
        task_id = body["id"]
        # The vault file already exists (created by the earlier
        # test) — unlock it, don't re-create it.
        status, body = _request(
            self.base, "POST", "/vault/unlock",
            body={"password": PASSWORD, "task_id": task_id},
        )
        self.assertEqual(status, 200, body)
        status, body = _request(
            self.base, "POST", "/vault/store-password",
            body={"task_id": task_id, "origin": "http://localhost",
                  "field": "card_number", "value": "4111 1111 1111 1111"},
        )
        self.assertEqual(status, 400, body)
        status, body = _request(
            self.base, "POST", "/vault/store-password",
            body={"task_id": task_id, "origin": "http://localhost",
                  "field": "password", "value": "hunter2"},
        )
        self.assertEqual(status, 200, body)
