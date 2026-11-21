"""Sprint 5 tests: cold mail (drafts, approval, dedup, send, log)
and the checkout/login flow on the checkout fixture.

Run: .venv/bin/python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import os
import socket as _socket
import subprocess as _subprocess
import sys as _sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from server.core import config, db
from server.jobs import profile
from server.mail import drafts as draft_mod
from server.mail import send as send_mod
from server.tasks import loop
from server.vault import fill as fill_mod
from server.vault import grants as grant_store
from server.vault.store import Vault

ROLE = {
    "id": "role_1", "title": "Software Development Intern",
    "company": "Acme Tech", "stipend": 15000, "location": "Remote",
}

RESUME_TEXT = """Test User
B.Tech IT, 3rd year, CGPA 8.75
Skills: Python, JavaScript, React, Node.js, SQL, C++
Email: test.user@example.com
Phone: 99999 99999
Looking for SDE intern roles, stipend above 10000, Remote or Hyderabad.
"""


class TestColdMail(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old = config.DATA_DIR
        config.DATA_DIR = Path(self.tmp.name)
        # Sprint 7: send_draft defaults to the real Gmail
        # transport — tests must use the mock one.
        self._old_transport = os.environ.get("ECLIPSE_MAIL_TRANSPORT")
        os.environ["ECLIPSE_MAIL_TRANSPORT"] = "mock"
        db.migrate()
        self.conn = db.connect()
        self.task_id = loop.create_task(self.conn, "Cold-mail recruiters")

    def tearDown(self):
        self.conn.close()
        config.DATA_DIR = self._old
        if self._old_transport is None:
            os.environ.pop("ECLIPSE_MAIL_TRANSPORT", None)
        else:
            os.environ["ECLIPSE_MAIL_TRANSPORT"] = self._old_transport
        self.tmp.cleanup()

    def test_draft_names_the_role_and_the_reasons(self):
        reasons = ["stipend ₹15,000/month", "location Remote", "CGPA 8.75"]
        draft_id = draft_mod.create_draft(
            self.conn, self.task_id, "recruiter@acme.example", ROLE,
            profile.TEST_PROFILE, reasons,
        )
        self.assertIsNotNone(draft_id)
        draft = draft_mod.get_draft(self.conn, draft_id)
        self.assertEqual(draft["status"], "draft")
        self.assertIn("Software Development Intern", draft["subject"])
        self.assertIn("Acme Tech", draft["subject"])
        self.assertIn("Test User", draft["body"])
        self.assertIn("stipend", draft["body"].lower())
        self.assertIn("resume", draft["body"].lower())

    def test_no_duplicate_mail_to_the_same_recruiter_for_the_same_role(self):
        first = draft_mod.create_draft(
            self.conn, self.task_id, "recruiter@acme.example", ROLE,
            profile.TEST_PROFILE, [],
        )
        self.assertIsNotNone(first)
        second = draft_mod.create_draft(
            self.conn, self.task_id, "recruiter@acme.example", ROLE,
            profile.TEST_PROFILE, [],
        )
        self.assertIsNone(second)
        # A different role for the same recruiter is fine.
        other = draft_mod.create_draft(
            self.conn, self.task_id, "recruiter@acme.example",
            {**ROLE, "id": "role_2", "title": "Backend Intern"},
            profile.TEST_PROFILE, [],
        )
        self.assertIsNotNone(other)

    def test_send_only_after_approval(self):
        draft_id = draft_mod.create_draft(
            self.conn, self.task_id, "recruiter@acme.example", ROLE,
            profile.TEST_PROFILE, [],
        )
        with self.assertRaises(ValueError):
            send_mod.send_draft(self.conn, draft_id)
        self.assertTrue(draft_mod.approve_draft(self.conn, draft_id))
        result = send_mod.send_draft(self.conn, draft_id)
        self.assertEqual(result.status, "sent")
        draft = draft_mod.get_draft(self.conn, draft_id)
        self.assertEqual(draft["status"], "sent")
        log = send_mod.mail_log(self.conn, self.task_id)
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["status"], "sent")

    def test_bounce_and_no_reply_are_logged(self):
        bounce_id = draft_mod.create_draft(
            self.conn, self.task_id, "bounce@acme.example", ROLE,
            profile.TEST_PROFILE, [],
        )
        draft_mod.approve_draft(self.conn, bounce_id)
        result = send_mod.send_draft(self.conn, bounce_id)
        self.assertEqual(result.status, "bounced")
        noreply_id = draft_mod.create_draft(
            self.conn, self.task_id, "noreply@acme.example",
            {**ROLE, "id": "role_2"}, profile.TEST_PROFILE, [],
        )
        draft_mod.approve_draft(self.conn, noreply_id)
        result = send_mod.send_draft(self.conn, noreply_id)
        self.assertEqual(result.status, "no_reply")
        log = send_mod.mail_log(self.conn, self.task_id)
        statuses = [entry["status"] for entry in log]
        self.assertEqual(statuses, ["bounced", "no_reply"])

    def test_edit_a_draft_before_approval(self):
        draft_id = draft_mod.create_draft(
            self.conn, self.task_id, "recruiter@acme.example", ROLE,
            profile.TEST_PROFILE, [],
        )
        self.assertTrue(draft_mod.update_draft_body(
            self.conn, draft_id, "Edited body."))
        draft = draft_mod.get_draft(self.conn, draft_id)
        self.assertEqual(draft["body"], "Edited body.")


class TestCheckoutFlow(unittest.TestCase):
    """The Sprint 5 checkout flow on the checkout fixture: fill
    the card from the per-task vault grant, approve the payment,
    verify by the URL changing to the confirmation page."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old = config.DATA_DIR
        config.DATA_DIR = Path(self.tmp.name)
        db.migrate()
        self.conn = db.connect()
        # The loop uses the process-wide VAULT (server/api/vault_api.py),
        # so the test unlocks that same instance.
        from server.api.vault_api import VAULT

        self.vault = VAULT
        profile.store_profile(self.conn, profile.TEST_PROFILE)

    def tearDown(self):
        self.conn.close()
        config.DATA_DIR = self._old
        self.tmp.cleanup()
        self.vault.lock()
        grant_store.GRANTS.revoke_all()

    def test_checkout_flow(self):
        # 1. The user unlocks the vault for this task and enters
        #    the card (per task, never stored).
        self.vault.create("correct horse battery staple")
        grant = grant_store.GRANTS.create(
            "task-checkout",
            card={"number": "4111 1111 1111 1111", "expiry": "12/28", "cvv": "123"},
        )
        self.assertEqual(grant.card_last4, "1111")
        # 2. The task: buy the book. The planner fills the card
        #    fields (mock planner, checkout-fixture observation).
        task_id = loop.create_task(
            self.conn, "Buy this book with my saved card",
            constraints="allowed sites: localhost",
        )
        loop.plan_task(
            self.conn, task_id,
            call_fn=lambda prompt, **kw: (json.dumps([
                {"step": 1, "description": "type the card number into the card field",
                 "expected_end_state": "card field holds a value"},
                {"step": 2, "description": "type the CVV into the CVV field",
                 "expected_end_state": "CVV field holds a value"},
                {"step": 3, "description": "click the pay button",
                 "expected_end_state": "payment confirmed"},
            ]), "mock:planner", 5.0),
        )
        checkout_obs = {
            "url": "http://localhost/checkout.html",
            "elements": [
                {"id": "el_card_number", "tag": "input", "role": "textbox",
                 "name": "Card number", "text": "", "value": "",
                 "visible": True, "interactable": True},
                {"id": "el_card_cvv", "tag": "input", "role": "textbox",
                 "name": "CVV", "text": "", "value": "",
                 "visible": True, "interactable": True},
                {"id": "el_pay", "tag": "button", "role": "button",
                 "name": "Pay now", "text": "", "value": "",
                 "type": "submit", "in_form": True,
                 "visible": True, "interactable": True},
            ],
        }
        # Step 1: type the card number. The execute response
        # carries the fill map (the vault is unlocked + the task
        # is a purchase, so an approval is needed first — the
        # card fill needs the approval grant too).
        import time as _time

        self.conn.execute(
            "INSERT INTO grants (id, origin, scope, expires_at, created_at)"
            " VALUES (?, ?, 'approve', ?, ?)",
            (uuid.uuid4().hex[:12], task_id, int(_time.time()) + 600,
             int(_time.time())),
        )
        self.conn.commit()
        # The vault grant must be for THIS task (the unlock above
        # used a different task id) — create it for the real task.
        grant_store.GRANTS.create(
            task_id,
            card={"number": "4111 1111 1111 1111", "expiry": "12/28", "cvv": "123"},
        )
        r1 = loop.execute_step(
            self.conn, task_id, 1, checkout_obs,
            call_fn=lambda prompt, **kw: (
                json.dumps({"action": "type", "element_id": "el_card_number",
                            "text": "[CARD_NUMBER]"}), "mock:planner", 5.0),
        )
        self.assertEqual(r1["action"]["element_id"], "el_card_number")
        self.assertEqual(r1.get("fill", {}).get("[CARD_NUMBER]"),
                         "4111 1111 1111 1111", r1)
        # Step 2: the CVV.
        r2 = loop.execute_step(
            self.conn, task_id, 2, checkout_obs,
            call_fn=lambda prompt, **kw: (
                json.dumps({"action": "type", "element_id": "el_card_cvv",
                            "text": "[CVV]"}), "mock:planner", 5.0),
        )
        self.assertEqual(r2.get("fill", {}).get("[CVV]"), "123", r2)
        # Step 3: the pay button needs an approval grant — the one
        # inserted above was consumed by step 1's card fill? No:
        # the fill map needs an approval grant but does not consume
        # it. The pay click consumes it.
        r3 = loop.execute_step(
            self.conn, task_id, 3, checkout_obs,
            call_fn=lambda prompt, **kw: (
                json.dumps({"action": "click", "element_id": "el_pay",
                            "reason": "pay for the book"}), "mock:planner", 5.0),
        )
        self.assertEqual(r3["action"]["element_id"], "el_pay")
        # Verify by the URL changing to the confirmation page.
        done_obs = {"url": "http://localhost/confirmation.html", "elements": []}
        v3 = loop.verify_step(
            self.conn, task_id, 3, done_obs, r3["expected_end_state"],
            previous_url=checkout_obs["url"], previous_observation=checkout_obs,
        )
        self.assertTrue(v3["verified"], v3)
        # The vault grant's card is wiped when the task's grants
        # are revoked (the card never reaches the vault file).
        grant_store.GRANTS.revoke_task(task_id)
        self.assertIsNone(grant_store.GRANTS.get(task_id))


if __name__ == "__main__":
    unittest.main()


# --- API tests (live server) ---

import socket as _socket
import subprocess as _subprocess
import sys as _sys
import urllib.error as _urlerror
import urllib.request as _urlrequest

_MAIL_TOKEN = "test-token-mail"
_REPO_ROOT = Path(__file__).parent.parent


def _free_port() -> int:
    with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _request(base_url, method, path, token=_MAIL_TOKEN, body=None):
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


class TestMailApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.port = _free_port()
        cls.base = f"http://127.0.0.1:{cls.port}"
        env = dict(os.environ)
        env["ECLIPSE_DATA_DIR"] = cls.tmp.name
        env["ECLIPSE_TOKEN"] = _MAIL_TOKEN
        # Sprint 7: the send endpoint defaults to the real Gmail
        # transport — the API test must use the mock one.
        env["ECLIPSE_MAIL_TRANSPORT"] = "mock"
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
            raise RuntimeError("server did not come up for mail API test")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(timeout=10)
        cls.tmp.cleanup()

    def test_draft_approve_send_flow(self):
        # A task + a profile (the resume) first.
        status, body = _request(
            self.base, "POST", "/tasks",
            body={"goal": "Cold-mail recruiters for the shortlisted roles"},
        )
        self.assertEqual(status, 201, body)
        task_id = body["id"]
        status, body = _request(
            self.base, "POST", "/jobs/profile",
            body={"resume_text": RESUME_TEXT},
        )
        self.assertEqual(status, 200, body)
        # Draft one mail (the full role, from the shortlist).
        role = {
            "id": "role_1", "title": "Software Development Intern",
            "company": "Acme Tech", "stipend": 15000, "location": "Remote",
            "deadline": "2026-12-03", "skills": ["Python"],
            "min_year": 3, "min_cgpa": 8.0, "requires_experience": False,
        }
        status, body = _request(
            self.base, "POST", "/mail/drafts",
            body={"task_id": task_id, "recipients": [
                {"email": "recruiter@acme.example", "role": role}]},
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(len(body["created"]), 1, body)
        self.assertEqual(len(body["skipped"]), 0, body)
        draft_id = body["created"][0]["draft_id"]
        # The draft names the real role (B1: no fabricated title).
        status, body = _request(
            self.base, "GET", f"/mail/drafts?task_id={task_id}",
        )
        self.assertEqual(status, 200, body)
        draft = body["drafts"][0]
        self.assertIn("Software Development Intern", draft["subject"])
        self.assertIn("Acme Tech", draft["subject"])
        self.assertIn("Software Development Intern", draft["body"])
        self.assertIn("Acme Tech", draft["body"])
        self.assertIn("Test User", draft["body"])
        # A role that does not fit is skipped, never drafted (M6).
        status, body = _request(
            self.base, "POST", "/mail/drafts",
            body={"task_id": task_id, "recipients": [
                {"email": "recruiter@cheap.example", "role": {
                    "id": "role_6", "title": "Marketing Intern",
                    "company": "AdWorld", "stipend": 8000,
                    "location": "Remote", "deadline": None,
                    "skills": [], "min_year": 3, "min_cgpa": 8.0,
                    "requires_experience": False}}]},
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(len(body["created"]), 0, body)
        self.assertEqual(len(body["skipped"]), 1, body)
        # A duplicate draft for the same pair is skipped.
        status, body = _request(
            self.base, "POST", "/mail/drafts",
            body={"task_id": task_id, "recipients": [
                {"email": "recruiter@acme.example", "role": role}]},
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(len(body["created"]), 0, body)
        self.assertEqual(len(body["skipped"]), 1, body)
        # Send before approval is refused.
        status, body = _request(
            self.base, "POST", f"/mail/drafts/{draft_id}/send",
        )
        self.assertEqual(status, 409, body)
        # Approve, then send.
        status, body = _request(
            self.base, "POST", f"/mail/drafts/{draft_id}/approve",
            body={"approval_id": "appr-mail-1"},
        )
        self.assertEqual(status, 200, body)
        status, body = _request(
            self.base, "POST", f"/mail/drafts/{draft_id}/send",
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["status"], "sent", body)
        # The log records it.
        status, body = _request(
            self.base, "GET", f"/mail/log?task_id={task_id}",
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(len(body["log"]), 1)
        self.assertEqual(body["log"][0]["status"], "sent")
        # B3: one approval sends one draft. A second draft needs
        # its own approval — the first approval was consumed.
        status, body = _request(
            self.base, "POST", "/mail/drafts",
            body={"task_id": task_id, "recipients": [
                {"email": "recruiter2@acme.example", "role": role}]},
        )
        self.assertEqual(status, 200, body)
        draft2_id = body["created"][0]["draft_id"]
        status, body = _request(
            self.base, "POST", f"/mail/drafts/{draft2_id}/send",
        )
        self.assertEqual(status, 409, body)


class TestMailBlockers(unittest.TestCase):
    """Regression tests for the Sprint 5 review blockers."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old = config.DATA_DIR
        config.DATA_DIR = Path(self.tmp.name)
        # Sprint 7: send_draft defaults to the real Gmail
        # transport — tests must use the mock one.
        self._old_transport = os.environ.get("ECLIPSE_MAIL_TRANSPORT")
        os.environ["ECLIPSE_MAIL_TRANSPORT"] = "mock"
        db.migrate()
        self.conn = db.connect()
        self.task_id = loop.create_task(self.conn, "Cold-mail recruiters")

    def tearDown(self):
        self.conn.close()
        config.DATA_DIR = self._old
        if self._old_transport is None:
            os.environ.pop("ECLIPSE_MAIL_TRANSPORT", None)
        else:
            os.environ["ECLIPSE_MAIL_TRANSPORT"] = self._old_transport
        self.tmp.cleanup()
        grant_store.GRANTS.revoke_all()

    def test_bounced_address_blocks_a_retry(self):
        # Sprint 7 M4 (plan §7.1 wins over the Sprint 5 M2 call):
        # a bounce stops further sends to that address. A new
        # draft for the same (recipient, role) is refused; a
        # corrected address is a different recipient.
        first = draft_mod.create_draft(
            self.conn, self.task_id, "bounce@acme.example", ROLE,
            profile.TEST_PROFILE, [],
        )
        self.assertIsNotNone(first)
        draft_mod.approve_draft(self.conn, first)
        result = send_mod.send_draft(self.conn, first)
        self.assertEqual(result.status, "bounced")
        second = draft_mod.create_draft(
            self.conn, self.task_id, "bounce@acme.example", ROLE,
            profile.TEST_PROFILE, [],
        )
        self.assertIsNone(second)
        # A different role for the same bounced address is also
        # blocked — the mailbox does not exist.
        other_role = draft_mod.create_draft(
            self.conn, self.task_id, "bounce@acme.example",
            {**ROLE, "id": "role_2", "title": "Backend Intern"},
            profile.TEST_PROFILE, [],
        )
        self.assertIsNone(other_role)

    def test_concurrent_drafts_for_the_same_pair_yield_one_row(self):
        # B4: the unique index is the real dedup, not the check.
        # Each thread uses its own connection (sqlite3
        # connections are not thread-safe).
        import threading

        results = []

        def _make():
            conn = db.connect()
            try:
                results.append(draft_mod.create_draft(
                    conn, self.task_id, "recruiter@acme.example", ROLE,
                    profile.TEST_PROFILE, [],
                ))
            finally:
                conn.close()

        threads = [threading.Thread(target=_make) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        created = [r for r in results if r is not None]
        self.assertEqual(len(created), 1, results)
        rows = self.conn.execute(
            "SELECT COUNT(*) AS c FROM mail_drafts"
            " WHERE LOWER(recipient) = 'recruiter@acme.example'"
            " AND role_id = 'role_1'",
        ).fetchone()
        self.assertEqual(rows["c"], 1)

    def test_a_role_without_an_id_is_rejected(self):
        # M9: the dedup key needs a real id (no title fallback).
        with self.assertRaises(ValueError):
            draft_mod.create_draft(
                self.conn, self.task_id, "recruiter@acme.example",
                {"title": "Software Development Intern"},
                profile.TEST_PROFILE, [],
            )


class TestFillScope(unittest.TestCase):
    """Regression tests for the fill-scope blockers (B2, B3, M8)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old = config.DATA_DIR
        config.DATA_DIR = Path(self.tmp.name)
        db.migrate()
        self.conn = db.connect()
        from server.api.vault_api import VAULT

        self.vault = VAULT
        profile.store_profile(self.conn, profile.TEST_PROFILE)

    def tearDown(self):
        self.conn.close()
        config.DATA_DIR = self._old
        self.tmp.cleanup()
        self.vault.lock()
        grant_store.GRANTS.revoke_all()

    def _task_approval(self, task_id, detail=None):
        import time as _time

        self.conn.execute(
            "INSERT INTO grants (id, origin, scope, expires_at, created_at, detail)"
            " VALUES (?, ?, 'approve', ?, ?, ?)",
            (uuid.uuid4().hex[:12], task_id, int(_time.time()) + 600,
             int(_time.time()), json.dumps(detail or {})),
        )
        self.conn.commit()

    def test_a_mail_approval_does_not_authorise_a_card_fill(self):
        # B2: a grant whose detail names a draft_id is a mail
        # approval — it must never fill a card.
        self.vault.create("correct horse battery staple")
        grant_store.GRANTS.create(
            "task-1",
            card={"number": "4111 1111 1111 1111", "expiry": "12/28", "cvv": "123"},
        )
        self._task_approval("task-1", {"draft_id": "abc123"})
        fill, denied = fill_mod.build_fill_map(
            self.conn, "task-1", ["[CARD_NUMBER]", "[CVV]"],
            "http://localhost", self.vault,
            approval_grants=[{"id": "g1", "detail": json.dumps({"draft_id": "abc123"})}],
            target={"id": "el_card_number", "name": "Card number", "text": "", "value": "[CARD_NUMBER]"},
        )
        self.assertEqual(fill, {})
        self.assertIsNotNone(denied)

    def test_identity_fill_needs_the_job_hunt_or_an_approval(self):
        # M8: the planner cannot type the user's email into any
        # field on any page. The job-hunt apply flow (a "role:"
        # constraint) or an approval grant authorises it.
        fill, _denied = fill_mod.build_fill_map(
            self.conn, "task-1", ["[EMAIL_1]"],
            "http://localhost", self.vault,
            approval_grants=None, constraints="",
            target={"id": "el_email", "name": "Email", "text": "", "value": "[EMAIL_1]"},
        )
        self.assertEqual(fill, {})
        fill, _denied = fill_mod.build_fill_map(
            self.conn, "task-1", ["[EMAIL_1]"],
            "http://localhost", self.vault,
            approval_grants=None, constraints="role: role_1\nurl: http://localhost/jobs/role_1",
            target={"id": "el_email", "name": "Email", "text": "", "value": "[EMAIL_1]"},
        )
        self.assertEqual(fill["[EMAIL_1]"], "test.user@example.com")
        fill, _denied = fill_mod.build_fill_map(
            self.conn, "task-1", ["[EMAIL_1]"],
            "http://localhost", self.vault,
            approval_grants=[{"id": "g1"}], constraints="",
            target={"id": "el_email", "name": "Email", "text": "", "value": "[EMAIL_1]"},
        )
        self.assertEqual(fill["[EMAIL_1]"], "test.user@example.com")

    def test_expired_grant_card_is_wiped_on_status(self):
        # M1: status() must not leave an expired grant's card in
        # memory.
        grant = grant_store.GRANTS.create(
            "task-1",
            card={"number": "4111 1111 1111 1111", "expiry": "12/28", "cvv": "123"},
        )
        grant.expires_at = int(time.time()) - 1
        status = grant_store.GRANTS.status()
        self.assertEqual(status["grants"], [])
        self.assertIsNone(grant.card)


class TestFillDenied(unittest.TestCase):
    """Regression tests for the frontend B3/B4 fixes: approval
    before fill, and the placeholder↔element binding."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old = config.DATA_DIR
        config.DATA_DIR = Path(self.tmp.name)
        db.migrate()
        self.conn = db.connect()
        from server.api.vault_api import VAULT

        self.vault = VAULT
        profile.store_profile(self.conn, profile.TEST_PROFILE)

    def tearDown(self):
        self.conn.close()
        config.DATA_DIR = self._old
        self.tmp.cleanup()
        self.vault.lock()
        grant_store.GRANTS.revoke_all()

    def test_a_secret_type_without_approval_asks_the_user(self):
        # B3: the type steps are not consequential, so without
        # this the user is never prompted — the step fails 3
        # times and asks with a generic failure. Now execute_step
        # raises the approval-needed error and the task goes to
        # ask_user with the reason.
        task_id = loop.create_task(
            self.conn, "Buy this book with my saved card",
            constraints="allowed sites: localhost",
        )
        loop.plan_task(
            self.conn, task_id,
            call_fn=lambda prompt, **kw: (json.dumps([
                {"step": 1, "description": "type the card number into the card field",
                 "expected_end_state": "card field holds a value"},
            ]), "mock:planner", 5.0),
        )
        checkout_obs = {
            "url": "http://localhost/checkout.html",
            "elements": [
                {"id": "el_card_number", "tag": "input", "role": "textbox",
                 "name": "Card number", "text": "", "value": "",
                 "visible": True, "interactable": True},
            ],
        }
        mock_action = lambda prompt, **kw: (json.dumps({
            "action": "type", "element_id": "el_card_number",
            "text": "[CARD_LAST4]", "reason": "fill the card number",
        }), "mock:planner", 5.0)
        with self.assertRaises(ValueError) as ctx:
            loop.execute_step(
                self.conn, task_id, 1, checkout_obs, call_fn=mock_action,
            )
        self.assertIn("approval needed before the vault can fill", str(ctx.exception))
        task = loop.get_task(self.conn, task_id)
        self.assertEqual(task["status"], "ask_user")

    def test_a_card_token_in_the_wrong_field_is_not_filled(self):
        # B4: the token must belong to the target element. A card
        # token typed into a "Notes" field is refused.
        self.vault.create("correct horse battery staple")
        grant_store.GRANTS.create(
            "task-1",
            card={"number": "4111 1111 1111 1111", "expiry": "12/28", "cvv": "123"},
        )
        fill, denied = fill_mod.build_fill_map(
            self.conn, "task-1", ["[CARD_LAST4]"],
            "http://localhost", self.vault,
            approval_grants=[{"id": "g1"}],
            target={"id": "el_notes", "name": "Notes", "text": "", "value": ""},
        )
        self.assertEqual(fill, {})
        self.assertIsNotNone(denied)

    def test_the_card_grant_is_scoped_to_its_origin(self):
        # B4 (deeper): a grant unlocked for one origin does not
        # fill on another origin.
        self.vault.create("correct horse battery staple")
        grant_store.GRANTS.create(
            "task-1",
            card={"number": "4111 1111 1111 1111", "expiry": "12/28", "cvv": "123"},
            origin="http://localhost",
        )
        fill, denied = fill_mod.build_fill_map(
            self.conn, "task-1", ["[CARD_LAST4]"],
            "http://evil.example", self.vault,
            approval_grants=[{"id": "g1"}],
            target={"id": "el_card_number", "name": "Card number", "text": "", "value": "[CARD_LAST4]"},
        )
        self.assertEqual(fill, {})
        self.assertIn("evil.example", denied or "")
