"""Audit regression tests (coordinator integration fixes, 2026-11-22).

Each test failed on the code as audited and passes after the fix.
These are the cross-area findings the flaw-hunt workers surfaced
for integration — the Gmail enabled-check bypass and the receipts
defence-in-depth index. (The watch-check privacy bypass, X1, was
investigated and deliberately NOT fixed: redacting the value
before comparison breaks the M11 behaviour — a PII-bearing value
changing must still fire — and the raw last_value is a deliberate
design choice; see the audit report.)

Run: .venv/bin/python -m unittest tests.test_audit_integration -v
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from server.connectors import registry as conn_registry
from server.core import config, db
from server.jobs import profile, receipts
from server.mail import drafts as draft_mod
from server.mail import send as send_mod
from server.tasks import loop

ROLE = {
    "id": "role_1", "title": "Software Development Intern",
    "company": "Acme Tech", "stipend": 15000, "location": "Remote",
}


class _DbCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old = config.DATA_DIR
        config.DATA_DIR = Path(self.tmp.name)
        self._old_transport = os.environ.get("ECLIPSE_MAIL_TRANSPORT")
        os.environ["ECLIPSE_MAIL_TRANSPORT"] = "mock"
        db.migrate()
        self.conn = db.connect()

    def tearDown(self):
        self.conn.close()
        config.DATA_DIR = self._old
        if self._old_transport is None:
            os.environ.pop("ECLIPSE_MAIL_TRANSPORT", None)
        else:
            os.environ["ECLIPSE_MAIL_TRANSPORT"] = self._old_transport
        self.tmp.cleanup()

    def _task(self, goal="Apply"):
        return loop.create_task(self.conn, goal)

    def _approved_draft(self, task_id):
        draft_id = draft_mod.create_draft(
            self.conn, task_id, "recruiter@acme.example", ROLE,
            profile.TEST_PROFILE, [],
        )
        # The approval machinery is not under test here — only
        # the enabled-check that send_draft must run before the
        # transport is built — so the status is set directly.
        self.conn.execute(
            "UPDATE mail_drafts SET status = 'approved' WHERE id = ?",
            (draft_id,),
        )
        self.conn.commit()
        return draft_id


class TestGmailDisabledStopsSends(_DbCase):
    """I1: disabling Gmail in the registry must stop Eclipse
    sending mail through it. The connectors API enforces the
    flag for its own endpoints (_require_on), but send_draft
    built GmailConnector directly, so a disabled Gmail still
    sent — and check_bounces_for_task still read the inbox."""

    def test_send_refused_when_gmail_is_off(self):
        task_id = self._task()
        draft_id = self._approved_draft(task_id)
        conn_registry.set_enabled(self.conn, "gmail", False)
        with self.assertRaises(ValueError) as ctx:
            send_mod.send_draft(self.conn, draft_id)
        self.assertIn("gmail", str(ctx.exception).lower())

    def test_send_works_when_gmail_is_on(self):
        task_id = self._task()
        draft_id = self._approved_draft(task_id)
        result = send_mod.send_draft(self.conn, draft_id)
        self.assertEqual(result.status, "sent")

    def test_bounce_check_refused_when_gmail_is_off(self):
        task_id = self._task()
        draft_id = self._approved_draft(task_id)
        self.conn.execute(
            "INSERT INTO mail_log (id, draft_id, recipient, role_id,"
            " status, detail, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("log_1", draft_id, "recruiter@acme.example", "role_1",
             "sent", "250 OK (mock)", 0),
        )
        self.conn.commit()
        conn_registry.set_enabled(self.conn, "gmail", False)
        with self.assertRaises(ValueError) as ctx:
            send_mod.check_bounces_for_task(self.conn, task_id)
        self.assertIn("gmail", str(ctx.exception).lower())

    def test_bounce_check_with_injected_connector_is_unaffected(self):
        # The check guards Eclipse's own Gmail use. A caller that
        # supplies its own connector (tests, a future caller) is
        # responsible for its own enablement — the injected path
        # is not gated.
        task_id = self._task()
        draft_id = self._approved_draft(task_id)
        self.conn.execute(
            "INSERT INTO mail_log (id, draft_id, recipient, role_id,"
            " status, detail, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("log_1", draft_id, "recruiter@acme.example", "role_1",
             "sent", "250 OK (mock)", 0),
        )
        self.conn.commit()
        conn_registry.set_enabled(self.conn, "gmail", False)

        class _FakeGmail:
            def check_bounce(self, recipient):
                return {"bounced": False, "detail": "no bounce"}

        results = send_mod.check_bounces_for_task(
            self.conn, task_id, gmail=_FakeGmail()
        )
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["bounced"])


class TestReceiptsSubmittedIndex(_DbCase):
    """I6: defence in depth behind the loop-level role claim and
    the idempotent receipt write — the database itself must
    refuse a second SUBMITTED receipt for one role, whatever path
    tried to write it. migrate() dedupes older duplicates first
    (keeping the newest), or the index creation would fail and
    the server would not start on an existing DB."""

    def _raw_receipt(self, receipt_id, task_id, role_id, status):
        self.conn.execute(
            "INSERT INTO receipts (id, task_id, role_id, title,"
            " company, url, status, confirmation, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (receipt_id, task_id, role_id, "SDE Intern", "Acme",
             "http://localhost/jobs/role_1", status, "ok", 0),
        )
        self.conn.commit()

    def test_migrate_dedupes_older_submitted_receipts(self):
        # Rebuild the pre-index state: drop the index, write the
        # duplicates the old bug allowed, then migrate.
        self.conn.execute(
            "DROP INDEX IF EXISTS idx_receipts_role_submitted"
        )
        self.conn.commit()
        first_task = self._task("Apply A")
        second_task = self._task("Apply B")
        self._raw_receipt("rcpt_old", first_task, "role_1", "submitted")
        self._raw_receipt("rcpt_new", second_task, "role_1", "submitted")
        db.migrate()
        rows = self.conn.execute(
            "SELECT id FROM receipts WHERE role_id = ?"
            " AND status = 'submitted'",
            ("role_1",),
        ).fetchall()
        self.assertEqual([r["id"] for r in rows], ["rcpt_new"])

    def test_a_second_submitted_receipt_is_refused_by_the_db(self):
        task_id = self._task()
        receipts.record_receipt(
            self.conn, task_id, "role_1", "SDE Intern", "Acme",
            "http://localhost/jobs/role_1", "Submitted.",
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self._raw_receipt(
                "rcpt_dup", self._task("Apply B"), "role_1", "submitted"
            )

    def test_a_failed_receipt_for_the_same_role_still_inserts(self):
        # The index is partial: only 'submitted' rows are unique
        # per role. Failed attempts must still be recordable.
        task_id = self._task()
        receipts.record_receipt(
            self.conn, task_id, "role_1", "SDE Intern", "Acme",
            "http://localhost/jobs/role_1", "Submitted.",
        )
        self._raw_receipt(
            "rcpt_failed", self._task("Apply B"), "role_1", "failed"
        )
        rows = receipts.list_receipts(self.conn, task_id)
        self.assertEqual(len(rows), 1)

    def test_migrate_is_idempotent_with_the_index(self):
        db.migrate()
        db.migrate()
        task_id = self._task()
        receipts.record_receipt(
            self.conn, task_id, "role_1", "SDE Intern", "Acme",
            "http://localhost/jobs/role_1", "Submitted.",
        )
        self.assertIsNotNone(
            receipts.receipt_for_role(self.conn, "role_1")
        )


if __name__ == "__main__":
    unittest.main()
