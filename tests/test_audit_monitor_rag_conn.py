"""Audit regression tests (worker 5 area): monitor watches +
briefing, keyword-memory forget, connectors approvals/freebusy/
resume ranking.

Each test fails on the pre-audit code and passes after the fix.
Run: .venv/bin/python -m unittest tests.test_audit_monitor_rag_conn -v
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from server.core import config, db
from server.monitor import briefing as briefing_mod
from server.monitor import watches as watch_mod


class _TmpDb(unittest.TestCase):
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


class NoGmail:
    def search_offers(self, n):
        return []


class TestExpiredMuteResumes(_TmpDb):
    def _watch(self):
        w = watch_mod.create_watch(
            self.conn, "http://localhost/x", "#s", "changed", 15
        )
        watch_mod.check_watch(self.conn, w["id"], True, "A")
        return w

    def test_expired_mute_fires_again(self):
        # W1: due_watches hands an expired-mute watch out as due,
        # so check_watch must not refuse it as "muted" forever.
        w = self._watch()
        watch_mod.mute_watch(self.conn, w["id"], 1)
        self.conn.execute(
            "UPDATE watches SET muted_until = ? WHERE id = ?",
            (watch_mod._now() - 60, w["id"]),
        )
        self.conn.commit()
        result = watch_mod.check_watch(self.conn, w["id"], True, "B")
        self.assertTrue(result["fired"])
        self.assertEqual(result["status"], "active")
        after = watch_mod.get_watch(self.conn, w["id"])
        self.assertEqual(after["status"], "active")
        self.assertIsNone(after["muted_until"])

    def test_mute_still_in_force_still_refuses(self):
        # Guard: the fix must not unmute a live mute.
        w = self._watch()
        watch_mod.mute_watch(self.conn, w["id"], 60)
        result = watch_mod.check_watch(self.conn, w["id"], True, "B")
        self.assertFalse(result["fired"])
        self.assertEqual(result["reason"], "muted")


class TestDisappearedCooldown(_TmpDb):
    def test_disappeared_does_not_realert_inside_the_hour(self):
        # W2 (M18): the 1-hour cooldown applies to every
        # condition — a flapping element must not alert on
        # every check.
        w = watch_mod.create_watch(
            self.conn, "http://localhost/x", "#s", "disappeared", 15
        )
        watch_mod.check_watch(self.conn, w["id"], True, "X")
        first = watch_mod.check_watch(self.conn, w["id"], False, None)
        self.assertTrue(first["fired"])
        watch_mod.check_watch(self.conn, w["id"], True, "X")
        second = watch_mod.check_watch(self.conn, w["id"], False, None)
        self.assertFalse(second["fired"])
        self.assertIn("cooldown", second["reason"])

    def test_disappeared_fires_again_after_the_hour(self):
        # Guard: the cooldown suppresses re-alerts inside the
        # hour only — a genuine disappearance later still fires.
        w = watch_mod.create_watch(
            self.conn, "http://localhost/x", "#s", "disappeared", 15
        )
        watch_mod.check_watch(self.conn, w["id"], True, "X")
        watch_mod.check_watch(self.conn, w["id"], False, None)
        watch_mod.check_watch(self.conn, w["id"], True, "X")
        watch_mod.check_watch(self.conn, w["id"], False, None)
        # An hour passes; the element is back, then gone again.
        watch_mod.check_watch(self.conn, w["id"], True, "X")
        self.conn.execute(
            "UPDATE watches SET last_fired_at = ? WHERE id = ?",
            (watch_mod._now() - 3601, w["id"]),
        )
        self.conn.commit()
        again = watch_mod.check_watch(self.conn, w["id"], False, None)
        self.assertTrue(again["fired"])


class TestBriefingCrowding(_TmpDb):
    def test_chatty_watch_does_not_hide_other_watches(self):
        # B1: the 200-row event window applied before the
        # per-watch dedup, so one watch firing 200+ times hid
        # every other watch's firing from the briefing.
        a = watch_mod.create_watch(
            self.conn, "http://localhost/a", "#s", "changed", 15
        )
        b = watch_mod.create_watch(
            self.conn, "http://localhost/b", "#s", "changed", 15
        )
        watch_mod.check_watch(self.conn, b["id"], True, "x")
        watch_mod.check_watch(self.conn, b["id"], True, "y")
        watch_mod.check_watch(self.conn, a["id"], True, "v0")
        for i in range(1, 210):
            watch_mod.check_watch(self.conn, a["id"], True, f"v{i}")
        briefing = briefing_mod.build_briefing(self.conn, gmail=NoGmail())
        titles = [c["title"] for c in briefing["cards"]]
        self.assertTrue(any("localhost/a" in t for t in titles))
        self.assertTrue(any("localhost/b" in t for t in titles))


class TestBriefingStaleBroken(_TmpDb):
    def test_recovered_watch_gets_no_broken_card(self):
        # B2: a watch that broke and recovered must not show a
        # "Watch broken" card claiming the element is gone.
        w = watch_mod.create_watch(
            self.conn, "http://localhost/x", "#s", "changed", 15
        )
        watch_mod.check_watch(self.conn, w["id"], True, "A")
        for _ in range(3):
            watch_mod.check_watch(self.conn, w["id"], False, None)
        watch_mod.check_watch(self.conn, w["id"], True, "A")
        self.assertEqual(
            watch_mod.get_watch(self.conn, w["id"])["status"], "active"
        )
        briefing = briefing_mod.build_briefing(self.conn, gmail=NoGmail())
        kinds = [c["kind"] for c in briefing["cards"]]
        self.assertNotIn("watch_broken", kinds)


class TestBriefingQuiet(_TmpDb):
    def test_empty_briefing_gets_a_quiet_card(self):
        # B3: never a silent empty briefing.
        briefing = briefing_mod.build_briefing(self.conn, gmail=NoGmail())
        self.assertEqual(len(briefing["cards"]), 1)
        self.assertEqual(briefing["cards"][0]["kind"], "quiet")


class TestBriefingDisabledGmail(_TmpDb):
    def test_briefing_does_not_scan_a_disabled_inbox(self):
        # B4: a disabled connector refuses work — the briefing
        # must not call Gmail when gmail is off.
        from server.connectors import registry as reg

        reg.set_enabled(self.conn, "gmail", False)

        class SpyGmail:
            def __init__(self):
                self.called = False

            def search_offers(self, n):
                self.called = True
                return []

        spy = SpyGmail()
        briefing = briefing_mod.build_briefing(self.conn, gmail=spy)
        self.assertFalse(spy.called)
        self.assertEqual(len(briefing["cards"]), 1)
        self.assertEqual(briefing["cards"][0]["kind"], "quiet")


class TestFactForget(_TmpDb):
    def test_delete_fact_removes_it_from_recall(self):
        # M1: there was no way to forget a keyword fact, though
        # the RAG store docstring claimed a DELETE endpoint.
        from server.memory import keyword

        fact_id = keyword.store_fact(
            self.conn, "the user prefers window seats", topic="travel"
        )
        self.assertEqual(
            len(keyword.recall(self.conn, "window seats")), 1
        )
        self.assertTrue(keyword.delete_fact(self.conn, fact_id))
        self.assertEqual(keyword.recall(self.conn, "window seats"), [])
        self.assertFalse(keyword.delete_fact(self.conn, fact_id))

    def test_delete_fact_api_route(self):
        from fastapi import HTTPException

        from server.api import memory_api
        from server.memory import keyword

        fact_id = keyword.store_fact(self.conn, "the user prefers tea")
        result = memory_api.delete_fact(fact_id, _token="x")
        self.assertEqual(result, {"ok": True, "id": fact_id})
        with self.assertRaises(HTTPException) as ctx:
            memory_api.delete_fact(fact_id, _token="x")
        self.assertEqual(ctx.exception.status_code, 404)


class TestApprovalCanonicalPath(_TmpDb):
    def setUp(self):
        super().setUp()
        from server.tasks import loop

        self.task_id = loop.create_task(self.conn, "Interview prep")
        from server.api import connectors_api

        self.api = connectors_api
        self._orig_drive = connectors_api.DriveConnector

    def tearDown(self):
        self.api.DriveConnector = self._orig_drive
        super().tearDown()

    def test_approval_with_dotdot_path_is_consumable(self):
        # C1: the approval stored the raw path while the upload
        # endpoint matched the resolved path, so an approval
        # naming `sub/../receipt.pdf` could never be consumed.
        home = Path.home()
        noncanon = str(home / "sub" / ".." / "receipt.pdf")
        body = self.api.ApprovalIn(
            task_id=self.task_id, kind="drive_upload", path=noncanon
        )
        approval = self.api.create_approval(body, _token="x")["approval_id"]

        seen = {}

        class FakeDrive:
            def upload_file(self, path, name=None):
                seen["path"] = path
                return {"id": "f1", "name": "receipt.pdf", "webViewLink": None}

        self.api.DriveConnector = FakeDrive
        result = self.api.drive_upload(
            self.api.UploadIn(
                task_id=self.task_id, approval_id=approval, path=noncanon
            ),
            _token="x",
        )
        self.assertEqual(result["id"], "f1")
        self.assertEqual(seen["path"], str(home / "receipt.pdf"))


class TestFreebusyMalformed(_TmpDb):
    def setUp(self):
        super().setUp()
        from server.api import connectors_api

        self.api = connectors_api
        self._orig_calendar = connectors_api.CalendarConnector

    def tearDown(self):
        self.api.CalendarConnector = self._orig_calendar
        super().tearDown()

    def test_non_dict_primary_is_a_502_not_a_500(self):
        # C2: a non-dict `primary` block crashed with
        # AttributeError (a 500) instead of the fail-closed 502.
        from fastapi import HTTPException

        class FakeCalendar:
            def freebusy(self, a, b):
                return {"calendars": {"primary": "garbage-string"}}

        self.api.CalendarConnector = FakeCalendar
        with self.assertRaises(HTTPException) as ctx:
            self.api.calendar_freebusy(
                "2026-11-22T09:00:00+05:30", "2026-11-22T10:00:00+05:30",
                _token="x",
            )
        self.assertEqual(ctx.exception.status_code, 502)

    def test_non_list_busy_is_a_502(self):
        from fastapi import HTTPException

        class FakeCalendar:
            def freebusy(self, a, b):
                return {"calendars": {"primary": {"busy": "not-a-list"}}}

        self.api.CalendarConnector = FakeCalendar
        with self.assertRaises(HTTPException) as ctx:
            self.api.calendar_freebusy(
                "2026-11-22T09:00:00+05:30", "2026-11-22T10:00:00+05:30",
                _token="x",
            )
        self.assertEqual(ctx.exception.status_code, 502)


def _fake_drive_cli(by_query):
    """A fake GwsCli transport: picks the file list by the Drive
    query string in argv."""
    from server.connectors.base import GwsCli

    def transport(argv, timeout):
        query = ""
        if "--params" in argv:
            query = json.loads(argv[argv.index("--params") + 1]).get("q", "")
        files = by_query.get(query, [])
        return {"ok": True, "data": {"files": files}, "raw": ""}

    return GwsCli(transport=transport)


class TestFindResumeRanking(unittest.TestCase):
    def _find(self, by_query):
        from server.connectors.drive import DriveConnector

        return DriveConnector(_fake_drive_cli(by_query)).find_resume()

    def test_newer_template_loses_to_resume_pdf(self):
        # C3: newest-first alone picked resume-template.docx
        # over the actual resume.
        resume = self._find({
            "name contains 'resume' and trashed=false": [
                {"id": "f1", "name": "resume.pdf",
                 "modifiedTime": "2026-01-01T00:00:00Z"},
                {"id": "f2", "name": "resume-template.docx",
                 "modifiedTime": "2026-11-01T00:00:00Z"},
            ],
        })
        self.assertEqual(resume["id"], "f1")

    def test_presumed_innocence_loses_to_resume_pdf(self):
        # C3b: "Presumed Innocence" contains "resume" as a
        # substring but is not a resume.
        resume = self._find({
            "name contains 'resume' and trashed=false": [
                {"id": "g1", "name": "resume.pdf",
                 "modifiedTime": "2026-01-01T00:00:00Z"},
                {"id": "g2", "name": "Presumed Innocence notes.pdf",
                 "modifiedTime": "2026-11-01T00:00:00Z"},
            ],
        })
        self.assertEqual(resume["id"], "g1")

    def test_substring_only_match_loses_to_a_real_cv(self):
        resume = self._find({
            "name contains 'resume' and trashed=false": [
                {"id": "h1", "name": "Presumed Innocence notes.pdf",
                 "modifiedTime": "2026-11-01T00:00:00Z"},
            ],
            "name contains 'CV' and trashed=false": [
                {"id": "h2", "name": "CV.pdf",
                 "modifiedTime": "2026-01-01T00:00:00Z"},
            ],
        })
        self.assertEqual(resume["id"], "h2")

    def test_newest_within_a_rank_still_wins(self):
        # Guard: ranking must not break the old newest-wins
        # behaviour when the names are equally good.
        resume = self._find({
            "name contains 'resume' and trashed=false": [
                {"id": "i1", "name": "resume-old.pdf",
                 "modifiedTime": "2025-01-01T00:00:00Z"},
                {"id": "i2", "name": "resume-2026.pdf",
                 "modifiedTime": "2026-11-01T00:00:00Z"},
            ],
        })
        self.assertEqual(resume["id"], "i2")


if __name__ == "__main__":
    unittest.main()
