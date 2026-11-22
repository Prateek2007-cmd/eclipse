"""Audit regression tests (jobs + mail): each test failed on the
code as audited and passes after the fix. One test per finding —
see the audit report for the flaw list (F1–F12).

Run: .venv/bin/python -m unittest tests.test_audit_jobs_mail -v
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path

from server.api import mail_api
from server.core import config, db
from server.jobs import matcher, profile, receipts
from server.mail import drafts as draft_mod
from server.mail import send as send_mod
from server.tasks import loop

TODAY = _dt.date.today()

ROLE = {
    "id": "role_1", "title": "Software Development Intern",
    "company": "Acme Tech", "stipend": 15000, "location": "Remote",
}


def _role(**kw):
    r = {
        "id": "role_1", "title": "SDE Intern", "company": "Acme",
        "site": "testjobs", "url": "http://localhost/jobs/role_1",
        "stipend": 15000, "location": "Remote",
        "deadline": str(TODAY + _dt.timedelta(days=10)),
        "skills": ["Python"], "min_year": 3, "min_cgpa": None,
        "requires_experience": False,
    }
    r.update(kw)
    return r


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

    def _draft(self, task_id, recipient="recruiter@acme.example", role=None):
        return draft_mod.create_draft(
            self.conn, task_id, recipient, role or ROLE,
            profile.TEST_PROFILE, [],
        )


class TestReceiptFailedThenSubmitted(_DbCase):
    """F1: a failed receipt must not block the retry's receipt."""

    def test_a_retry_upgrades_the_failed_receipt(self):
        task_id = self._task()
        failed_id = receipts.record_receipt(
            self.conn, task_id, "role_1", "SDE Intern", "Acme",
            "http://localhost/jobs/role_1", "site timeout", status="failed",
        )
        submitted_id = receipts.record_receipt(
            self.conn, task_id, "role_1", "SDE Intern", "Acme",
            "http://localhost/jobs/role_1", "Submitted ref 1", status="submitted",
        )
        # One row, upgraded in place — not a second row, and not a
        # stale "failed" row masquerading as the success.
        rows = receipts.list_receipts(self.conn, task_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], failed_id)
        self.assertEqual(submitted_id, failed_id)
        self.assertEqual(rows[0]["status"], "submitted")
        self.assertIn("Submitted ref 1", rows[0]["confirmation"])
        self.assertIsNotNone(receipts.receipt_for_role(self.conn, "role_1"))
        report = receipts.task_report(self.conn, task_id)
        self.assertEqual(len(report["submitted"]), 1)
        self.assertEqual(len(report["failed"]), 0)

    def test_a_submitted_receipt_is_never_downgraded(self):
        task_id = self._task()
        first = receipts.record_receipt(
            self.conn, task_id, "role_1", "SDE Intern", "Acme",
            "http://localhost/jobs/role_1", "Submitted.",
        )
        second = receipts.record_receipt(
            self.conn, task_id, "role_1", "SDE Intern", "Acme",
            "http://localhost/jobs/role_1", "timeout", status="failed",
        )
        self.assertEqual(first, second)
        rows = receipts.list_receipts(self.conn, task_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "submitted")


class TestReceiptCrossTaskIdempotency(_DbCase):
    """F2: one submitted receipt per role, across tasks."""

    def test_a_second_task_gets_the_existing_receipt(self):
        first_task = self._task("Apply A")
        second_task = self._task("Apply B")
        first = receipts.record_receipt(
            self.conn, first_task, "role_1", "SDE Intern", "Acme",
            "http://localhost/jobs/role_1", "Submitted.",
        )
        second = receipts.record_receipt(
            self.conn, second_task, "role_1", "SDE Intern", "Acme",
            "http://localhost/jobs/role_1", "Submitted.",
        )
        self.assertEqual(first, second)
        n = self.conn.execute(
            "SELECT COUNT(*) AS c FROM receipts"
            " WHERE role_id = 'role_1' AND status = 'submitted'"
        ).fetchone()["c"]
        self.assertEqual(n, 1)

    def test_two_tasks_racing_to_record_yield_one_receipt(self):
        first_task = self._task("Apply A")
        second_task = self._task("Apply B")
        results = {}

        def _record(task_id, key):
            conn = db.connect()
            try:
                results[key] = receipts.record_receipt(
                    conn, task_id, "role_1", "SDE Intern", "Acme",
                    "http://localhost/jobs/role_1", "Submitted.",
                )
            finally:
                conn.close()

        threads = [
            threading.Thread(target=_record, args=(first_task, "a")),
            threading.Thread(target=_record, args=(second_task, "b")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results["a"], results["b"])
        n = self.conn.execute(
            "SELECT COUNT(*) AS c FROM receipts"
            " WHERE role_id = 'role_1' AND status = 'submitted'"
        ).fetchone()["c"]
        self.assertEqual(n, 1)


class TestEditRevokesApproval(_DbCase):
    """F3: editing an approved draft revokes the approval."""

    def test_editing_an_approved_draft_resets_it_to_draft(self):
        task_id = self._task("Mail")
        draft_id = self._draft(task_id)
        self.assertTrue(draft_mod.approve_draft(self.conn, draft_id))
        self.assertTrue(draft_mod.update_draft_body(
            self.conn, draft_id, "Edited body."))
        draft = draft_mod.get_draft(self.conn, draft_id)
        self.assertEqual(draft["status"], "draft")
        self.assertEqual(draft["body"], "Edited body.")
        # The edited body cannot go out under the old approval.
        with self.assertRaises(ValueError):
            send_mod.send_draft(self.conn, draft_id,
                                transport=send_mod.mock_transport)

    def test_edit_via_the_api_revokes_the_grant(self):
        task_id = self._task("Mail")
        draft_id = self._draft(task_id)
        mail_api.approve_draft(draft_id, mail_api.ApproveIn(approval_id="appr-1"))

        def _grant_count():
            return self.conn.execute(
                "SELECT COUNT(*) AS c FROM grants WHERE origin = ?",
                (task_id,),
            ).fetchone()["c"]

        self.assertEqual(_grant_count(), 1)
        mail_api.edit_draft(draft_id, mail_api.EditIn(body="Edited body."))
        self.assertEqual(_grant_count(), 0)
        self.assertEqual(
            draft_mod.get_draft(self.conn, draft_id)["status"], "draft")


class TestReapprovalAfterExpiry(_DbCase):
    """F4: an approved draft whose grant expired can be approved again."""

    def test_reapprove_after_the_grant_expires_then_send(self):
        task_id = self._task("Mail")
        draft_id = self._draft(task_id)
        mail_api.approve_draft(draft_id, mail_api.ApproveIn(approval_id="appr-1"))
        # The grant expires (10 minutes pass).
        self.conn.execute(
            "UPDATE grants SET expires_at = ? WHERE origin = ?",
            (int(time.time()) - 1, task_id),
        )
        self.conn.commit()
        # Re-approval works now — the draft is no longer stuck.
        result = mail_api.approve_draft(
            draft_id, mail_api.ApproveIn(approval_id="appr-2"))
        self.assertEqual(result["status"], "approved")
        sent = mail_api.send_draft(draft_id)
        self.assertEqual(sent["status"], "sent")

    def test_reapprove_while_a_live_grant_exists_is_refused(self):
        task_id = self._task("Mail")
        draft_id = self._draft(task_id)
        mail_api.approve_draft(draft_id, mail_api.ApproveIn(approval_id="appr-1"))
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as ctx:
            mail_api.approve_draft(
                draft_id, mail_api.ApproveIn(approval_id="appr-2"))
        self.assertEqual(ctx.exception.status_code, 409)


class TestBounceBlocksTheSend(_DbCase):
    """F5: a bounced address is blocked at send time, not just at
    draft time."""

    def test_an_approved_draft_cannot_be_sent_to_a_bounced_address(self):
        task_id = self._task("Mail")
        # Draft B is created + approved BEFORE any bounce is known.
        draft_b = self._draft(
            task_id, "bounce@acme.example",
            {**ROLE, "id": "role_2", "title": "Backend Intern"},
        )
        draft_mod.approve_draft(self.conn, draft_b)
        # Draft A is sent and bounces; the bounce is recorded.
        draft_a = self._draft(task_id, "bounce@acme.example")
        draft_mod.approve_draft(self.conn, draft_a)
        result = send_mod.send_draft(self.conn, draft_a,
                                     transport=send_mod.mock_transport)
        self.assertEqual(result.status, "bounced")
        # The approved draft for the same address must not go out.
        with self.assertRaises(ValueError):
            send_mod.send_draft(self.conn, draft_b,
                                transport=send_mod.mock_transport)
        self.assertEqual(
            draft_mod.get_draft(self.conn, draft_b)["status"], "bounced")
        # No send was attempted for B: the log still has only A's row.
        log = send_mod.mail_log(self.conn, task_id)
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["status"], "bounced")


class TestFailedSendIsLogged(_DbCase):
    """F6: a failed send attempt is logged, and the draft can be retried."""

    def test_a_transport_failure_leaves_a_failed_log_row(self):
        task_id = self._task("Mail")
        draft_id = self._draft(task_id)
        draft_mod.approve_draft(self.conn, draft_id)

        def _boom(recipient, subject, body):
            raise RuntimeError("gmail unavailable (fake)")

        with self.assertRaises(RuntimeError):
            send_mod.send_draft(self.conn, draft_id, transport=_boom)
        log = send_mod.mail_log(self.conn, task_id)
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["status"], "failed")
        self.assertEqual(log[0]["draft_id"], draft_id)
        # The draft stays approved, so the send can be retried.
        self.assertEqual(
            draft_mod.get_draft(self.conn, draft_id)["status"], "approved")
        result = send_mod.send_draft(self.conn, draft_id,
                                     transport=send_mod.mock_transport)
        self.assertEqual(result.status, "sent")
        self.assertEqual(len(send_mod.mail_log(self.conn, task_id)), 2)


class TestFindResumePrefersTheResume(unittest.TestCase):
    """F7: the attachment is the resume, not the newest same-owner file."""

    def _write(self, user_dir, name, mtime):
        path = user_dir / name
        path.write_bytes(b"%PDF-1.4 fake")
        os.utime(path, (mtime, mtime))
        return path

    def test_a_resume_named_file_beats_newer_notes(self):
        with tempfile.TemporaryDirectory() as tmp:
            user_dir = Path(tmp)
            resume = self._write(user_dir, "resume-akash.pdf", 1000000000)
            self._write(user_dir, "akash-interview-notes.pdf", 1700000000)
            self.assertEqual(
                mail_api._find_resume(user_files=user_dir), str(resume))

    def test_a_cv_named_file_beats_newer_notes(self):
        with tempfile.TemporaryDirectory() as tmp:
            user_dir = Path(tmp)
            cv = self._write(user_dir, "Akash_CV.pdf", 1000000000)
            self._write(user_dir, "akash-interview-notes.pdf", 1700000000)
            self.assertEqual(
                mail_api._find_resume(user_files=user_dir), str(cv))

    def test_an_uppercase_pdf_extension_is_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            user_dir = Path(tmp)
            resume = self._write(user_dir, "Resume.PDF", 1000000000)
            self.assertEqual(
                mail_api._find_resume(user_files=user_dir), str(resume))

    def test_someone_elses_cv_is_never_a_candidate(self):
        # The original fix still holds: a "cv" in another person's
        # filename must not make their file attachable.
        with tempfile.TemporaryDirectory() as tmp:
            user_dir = Path(tmp)
            self._write(user_dir, "Harika_CV.pdf", 1700000000)
            self.assertIsNone(mail_api._find_resume(user_files=user_dir))


class TestShortlistDedup(unittest.TestCase):
    """F8: the same role twice in the input appears once in the shortlist."""

    def test_duplicate_roles_are_deduped_keeping_the_best_score(self):
        low = _role(stipend=12000)
        high = _role(stipend=20000)
        short = matcher.shortlist([low, high, low], profile.TEST_PROFILE, TODAY)
        self.assertEqual(len(short), 1)
        self.assertEqual(short[0]["role_id"], "role_1")
        self.assertEqual(
            short[0]["score"],
            matcher.match_role(high, profile.TEST_PROFILE, TODAY)["score"])

    def test_distinct_roles_are_kept(self):
        a = _role(id="role_1")
        b = _role(id="role_2", title="Backend Intern")
        short = matcher.shortlist([a, b], profile.TEST_PROFILE, TODAY)
        self.assertEqual(len(short), 2)


class TestRecipientNormalisation(_DbCase):
    """F9: the stored recipient is trimmed, so the unique index
    (LOWER, untrimmed) cannot be slipped past with whitespace."""

    def test_a_whitespace_recipient_is_stored_trimmed(self):
        task_id = self._task("Mail")
        draft_id = self._draft(task_id, "  recruiter@acme.example  ")
        self.assertIsNotNone(draft_id)
        self.assertEqual(
            draft_mod.get_draft(self.conn, draft_id)["recipient"],
            "recruiter@acme.example")

    def test_concurrent_whitespace_variants_yield_one_draft(self):
        task_id = self._task("Mail")
        results = []

        def _make(email):
            conn = db.connect()
            try:
                results.append(draft_mod.create_draft(
                    conn, task_id, email, ROLE, profile.TEST_PROFILE, []))
            finally:
                conn.close()

        threads = [
            threading.Thread(target=_make, args=("recruiter@acme.example",)),
            threading.Thread(target=_make, args=(" recruiter@acme.example ",)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        created = [r for r in results if r is not None]
        self.assertEqual(len(created), 1, results)


class TestMatcherEdgeCases(unittest.TestCase):
    """F10/F11: negative stipends and empty locations."""

    def test_a_negative_stipend_never_fits(self):
        prof = dict(profile.TEST_PROFILE, stipend_min=None)
        m = matcher.match_role(_role(stipend=-5000), prof, TODAY)
        self.assertFalse(m["fits"])
        self.assertTrue(m["missing"])

    def test_a_role_with_no_location_is_flagged_not_silently_fitted(self):
        m = matcher.match_role(_role(location=""), profile.TEST_PROFILE, TODAY)
        self.assertTrue(m["fits"], m)
        self.assertIn("location not listed", m["warnings"])
        self.assertNotIn("location ", m["reasons"])
        # No location points for a location that was never shown.
        with_loc = matcher.match_role(_role(), profile.TEST_PROFILE, TODAY)
        self.assertLess(m["score"], with_loc["score"])

    def test_a_stray_empty_location_entry_does_not_disable_the_filter(self):
        prof = dict(profile.TEST_PROFILE, locations=["", "Hyderabad"])
        m = matcher.match_role(_role(location="Chennai"), prof, TODAY)
        self.assertFalse(m["fits"])
        self.assertIn("chennai", m["missing"][0].lower())


class TestProfileCgpaValidation(unittest.TestCase):
    """F12: a CGPA above 10 is a percentage or a typo, not a CGPA."""

    def test_cgpa_above_ten_is_not_stored(self):
        p = profile.parse_resume("Test User\nB.Tech IT, 3rd year, CGPA 98\n")
        self.assertIsNone(p["cgpa"])

    def test_a_real_cgpa_is_still_parsed(self):
        p = profile.parse_resume("Test User\nB.Tech IT, 3rd year, CGPA 8.75\n")
        self.assertEqual(p["cgpa"], 8.75)


if __name__ == "__main__":
    unittest.main()
