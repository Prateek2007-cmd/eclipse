"""PR #5 (W3) tests: mission briefing cards + IST day guard +
force rebuild + scoped-fact lines.

Run: .venv/bin/python -m unittest tests.test_missions_briefing -v

The mission tables are created here (CREATE IF NOT EXISTS, the
§3.2 DDL) so this file passes whether or not W1's migration has
landed — after it lands the creates are no-ops. All test data
is fake.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from server.core import config, db
from server.monitor import briefing as briefing_mod
from server.monitor import watches as watch_mod

IST = ZoneInfo("Asia/Kolkata")
TOKEN = "test-token-briefing"

_MISSION_TABLES = """
CREATE TABLE IF NOT EXISTS missions(
  id TEXT PRIMARY KEY, type TEXT NOT NULL, name TEXT NOT NULL,
  status TEXT NOT NULL, config_json TEXT NOT NULL,
  bounds_json TEXT NOT NULL, created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL, last_run_at INTEGER,
  next_run_at INTEGER, last_run_status TEXT, last_error TEXT);
CREATE TABLE IF NOT EXISTS mission_runs(
  id TEXT PRIMARY KEY, mission_id TEXT NOT NULL, task_id TEXT,
  status TEXT NOT NULL, started_at INTEGER NOT NULL,
  ended_at INTEGER, detail TEXT);
CREATE TABLE IF NOT EXISTS mission_activity(
  id TEXT PRIMARY KEY, mission_id TEXT NOT NULL, run_id TEXT,
  kind TEXT NOT NULL, detail TEXT NOT NULL, created_at INTEGER NOT NULL);
"""


class NoGmail:
    def search_offers(self, n):
        return []


class FakeGmail:
    def search_offers(self, n):
        return [
            {"id": f"m{i}", "subject": f"Offer {i}",
             "body_redacted": "shortlisted"}
            for i in range(20)
        ]


def _ist_epoch(year, month, day, hour, minute):
    return int(datetime(year, month, day, hour, minute,
                        tzinfo=IST).timestamp())


class _MissionDbCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old = config.DATA_DIR
        config.DATA_DIR = Path(self.tmp.name)
        db.migrate()
        self.conn = db.connect()
        self.conn.executescript(_MISSION_TABLES)
        cols = {r[1] for r in
                self.conn.execute("PRAGMA table_info(tasks)")}
        if "mission_id" not in cols:
            self.conn.execute(
                "ALTER TABLE tasks ADD COLUMN mission_id TEXT")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        config.DATA_DIR = self._old
        self.tmp.cleanup()

    def _mission(self, mid, name="Hunt", status="active",
                 last_run_status=None, last_error=None):
        now = int(time.time())
        self.conn.execute(
            "INSERT INTO missions (id, type, name, status, config_json,"
            " bounds_json, created_at, updated_at, last_run_status,"
            " last_error) VALUES (?, 'job_hunt', ?, ?, '{}', '{}', ?, ?,"
            " ?, ?)",
            (mid, name, status, now, now, last_run_status, last_error),
        )
        self.conn.commit()

    def _activity(self, mid, kind, detail, created_at=None):
        if created_at is None:
            created_at = int(time.time())
        self._activity_n = getattr(self, "_activity_n", 0) + 1
        self.conn.execute(
            "INSERT INTO mission_activity (id, mission_id, run_id, kind,"
            " detail, created_at) VALUES (?, ?, NULL, ?, ?, ?)",
            (f"ma_{mid}_{kind}_{self._activity_n}", mid, kind,
             json.dumps(detail), created_at),
        )
        self.conn.commit()

    def _task(self, tid, mission_id):
        now = int(time.time())
        self.conn.execute(
            "INSERT INTO tasks (id, goal, constraints, status,"
            " created_at, updated_at, receipt, mission_id)"
            " VALUES (?, 'fake goal', '', 'planned', ?, ?, NULL, ?)",
            (tid, now, now, mission_id),
        )
        self.conn.commit()

    def _draft(self, did, task_id, status="draft"):
        now = int(time.time())
        self._draft_n = getattr(self, "_draft_n", 0) + 1
        # The (recipient, role) partial unique index blocks two
        # live drafts for the same pair — vary the recipient.
        recipient = f"recruiter{self._draft_n}@example.com"
        self.conn.execute(
            "INSERT INTO mail_drafts (id, task_id, recipient, role_id,"
            " subject, body, status, created_at, updated_at)"
            " VALUES (?, ?, ?, 'role_1', 'Hi',"
            " 'body', ?, ?, ?)",
            (did, task_id, recipient, status, now, now),
        )
        self.conn.commit()

    def _approval(self, aid, task_id, action="submit", live=True):
        now = int(time.time())
        expires = now + 600 if live else now - 10
        self.conn.execute(
            "INSERT INTO pending_approvals (id, task_id, step, action,"
            " detail, created_at, expires_at)"
            " VALUES (?, ?, 1, ?, '{}', ?, ?)",
            (aid, task_id, action, now, expires),
        )
        self.conn.commit()

    def _cards(self, **kwargs):
        briefing = briefing_mod.build_briefing(
            self.conn, gmail=kwargs.pop("gmail", NoGmail()), **kwargs)
        return briefing["cards"]

    def _kinds(self, cards):
        return [c["kind"] for c in cards]


class TestMissionCards(_MissionDbCase):
    def test_applied_card(self):
        self._mission("m_1", "SDE hunt")
        self._activity("m_1", "applied", {
            "title": "SDE Intern", "company": "Example Corp",
            "url": "https://example.com/jobs/1",
            "confirmation": "Application received",
            "role_id": "role_1",
        })
        cards = self._cards()
        applied = [c for c in cards if c["kind"] == "mission_applied"]
        self.assertEqual(len(applied), 1)
        self.assertEqual(applied[0]["title"],
                         "Applied to SDE Intern at Example Corp")
        self.assertIn("Application received", applied[0]["body"])
        self.assertEqual(applied[0]["action"],
                         "https://example.com/jobs/1")
        self.assertEqual(applied[0]["action_label"], "Open role")

    def test_applied_card_without_company(self):
        self._mission("m_1")
        self._activity("m_1", "applied", {"title": "SDE Intern"})
        cards = self._cards()
        applied = [c for c in cards if c["kind"] == "mission_applied"]
        self.assertEqual(applied[0]["title"], "Applied to SDE Intern")

    def test_applied_older_than_24h_ignored(self):
        self._mission("m_1")
        self._activity("m_1", "applied", {"title": "SDE Intern"},
                       created_at=int(time.time()) - 25 * 3600)
        cards = self._cards()
        self.assertNotIn("mission_applied", self._kinds(cards))
        self.assertEqual(self._kinds(cards), ["quiet"])

    def test_draft_ready_card(self):
        self._mission("m_1")
        self._task("t_1", "m_1")
        self._draft("d_1", "t_1")
        self._draft("d_2", "t_1")
        cards = self._cards()
        drafts = [c for c in cards if c["kind"] == "mission_draft_ready"]
        self.assertEqual(len(drafts), 1)
        self.assertIn("2 cold-mail drafts", drafts[0]["title"])
        self.assertEqual(drafts[0]["action"],
                         "http://127.0.0.1:8000/workspace")

    def test_draft_ready_ignores_sent_and_unlinked(self):
        self._mission("m_1")
        self._task("t_1", "m_1")
        self._task("t_2", None)
        self._draft("d_1", "t_1", status="sent")
        self._draft("d_2", "t_2", status="draft")
        cards = self._cards()
        self.assertNotIn("mission_draft_ready", self._kinds(cards))

    def test_approval_needed_card(self):
        self._mission("m_1", "SDE hunt")
        self._task("t_1", "m_1")
        self._approval("appr-1", "t_1")
        cards = self._cards()
        need = [c for c in cards
                if c["kind"] == "mission_approval_needed"]
        self.assertEqual(len(need), 1)
        self.assertIn("SDE hunt", need[0]["body"])
        self.assertIn("1 approval", need[0]["title"])

    def test_approval_needed_ignores_expired(self):
        self._mission("m_1")
        self._task("t_1", "m_1")
        self._approval("appr-1", "t_1", live=False)
        cards = self._cards()
        self.assertNotIn("mission_approval_needed", self._kinds(cards))

    def test_approval_needed_via_links_module(self):
        # W1's links.py contract (§4): when the module exists the
        # briefing uses pending_approvals_for_mission per mission.
        # Fake it here — the real module may not exist yet.
        self._mission("m_1", "SDE hunt")
        fake_links = types.ModuleType("server.missions.links")

        def _pending(conn, mission_id):
            if mission_id == "m_1":
                return [{"action": "submit", "approval_id": "appr-9"}]
            return []

        fake_links.pending_approvals_for_mission = _pending
        pkg = types.ModuleType("server.missions")
        pkg.__path__ = []
        saved = {k: sys.modules.get(k)
                 for k in ("server.missions", "server.missions.links")}
        sys.modules["server.missions"] = pkg
        sys.modules["server.missions.links"] = fake_links
        try:
            cards = self._cards()
        finally:
            for key, mod in saved.items():
                if mod is None:
                    sys.modules.pop(key, None)
                else:
                    sys.modules[key] = mod
        need = [c for c in cards
                if c["kind"] == "mission_approval_needed"]
        self.assertEqual(len(need), 1)
        self.assertIn("SDE hunt", need[0]["body"])

    def test_approval_needed_sql_fallback(self):
        # When links.py cannot be imported (None in sys.modules
        # raises ImportError), the briefing falls back to the
        # same join in SQL and the card still appears.
        self._mission("m_1", "SDE hunt")
        self._task("t_1", "m_1")
        self._approval("appr-1", "t_1")
        saved = sys.modules.get("server.missions.links")
        sys.modules["server.missions.links"] = None
        try:
            cards = self._cards()
        finally:
            if saved is None:
                sys.modules.pop("server.missions.links", None)
            else:
                sys.modules["server.missions.links"] = saved
        need = [c for c in cards
                if c["kind"] == "mission_approval_needed"]
        self.assertEqual(len(need), 1)
        self.assertIn("SDE hunt", need[0]["body"])

    def test_waiting_card(self):
        self._mission("m_1", "Price watch",
                      last_run_status="waiting_for_extension")
        cards = self._cards()
        waiting = [c for c in cards if c["kind"] == "mission_waiting"]
        self.assertEqual(len(waiting), 1)
        self.assertIn("Price watch", waiting[0]["body"])

    def test_waiting_ignores_paused(self):
        self._mission("m_1", status="paused",
                      last_run_status="waiting_for_extension")
        cards = self._cards()
        self.assertNotIn("mission_waiting", self._kinds(cards))

    def test_stalled_card(self):
        self._mission("m_1", "Cold mail", status="paused",
                      last_run_status="stalled",
                      last_error="Gmail connector is off")
        cards = self._cards()
        stalled = [c for c in cards if c["kind"] == "mission_stalled"]
        self.assertEqual(len(stalled), 1)
        self.assertIn("Cold mail", stalled[0]["title"])
        self.assertIn("Gmail connector is off", stalled[0]["body"])

    def test_stalled_ignores_healthy_paused(self):
        self._mission("m_1", status="paused", last_run_status="ok")
        cards = self._cards()
        self.assertNotIn("mission_stalled", self._kinds(cards))

    def test_max_cards_with_missions_and_offers(self):
        # Order: watch cards, then mission cards, then Gmail —
        # and MAX_CARDS = 10 still holds with all three competing.
        w = watch_mod.create_watch(
            self.conn, "http://localhost/result.html", "#result_status",
            "changed", interval_min=15)
        watch_mod.check_watch(self.conn, w["id"], True, "Not selected")
        watch_mod.check_watch(self.conn, w["id"], True, "Selected")
        self._mission("m_1")
        self._mission("m_2")
        self._activity("m_1", "applied", {"title": "Role A"})
        self._activity("m_2", "applied", {"title": "Role B"})
        cards = self._cards(gmail=FakeGmail())
        self.assertEqual(len(cards), 10)
        self.assertEqual(self._kinds(cards),
                         ["watch", "mission_applied", "mission_applied"]
                         + ["offer"] * 7)

    def test_mission_cards_capped_at_ten(self):
        self._mission("m_1")
        for i in range(12):
            self._activity("m_1", "applied", {"title": f"Role {i}"})
        cards = self._cards(gmail=FakeGmail())
        self.assertEqual(len(cards), 10)
        self.assertTrue(all(c["kind"] == "mission_applied"
                            for c in cards))

    def test_quiet_card_when_nothing_happened(self):
        cards = self._cards()
        self.assertEqual(self._kinds(cards), ["quiet"])
        self.assertEqual(cards[0]["title"], "Nothing new this morning")

    def test_no_mission_tables_still_builds(self):
        # Pre-PR #5 database: no missions tables at all — the
        # briefing degrades to watch + inbox cards, never crashes.
        self.conn.execute("DROP TABLE mission_activity")
        self.conn.execute("DROP TABLE mission_runs")
        self.conn.execute("DROP TABLE missions")
        self.conn.commit()
        cards = self._cards()
        self.assertEqual(self._kinds(cards), ["quiet"])


class TestDayGuard(_MissionDbCase):
    def test_second_build_same_day_returns_stored(self):
        first = briefing_mod.build_briefing(self.conn, gmail=NoGmail())
        second = briefing_mod.build_briefing(self.conn, gmail=NoGmail())
        self.assertEqual(second["id"], first["id"])
        count = self.conn.execute(
            "SELECT COUNT(*) AS n FROM briefings").fetchone()["n"]
        self.assertEqual(count, 1)

    def test_force_rebuilds_same_day(self):
        first = briefing_mod.build_briefing(self.conn, gmail=NoGmail())
        forced = briefing_mod.build_briefing(
            self.conn, gmail=NoGmail(), force=True)
        self.assertNotEqual(forced["id"], first["id"])
        count = self.conn.execute(
            "SELECT COUNT(*) AS n FROM briefings").fetchone()["n"]
        self.assertEqual(count, 2)

    def test_ist_boundary_0859_vs_0901(self):
        # Both inside one IST day: the 09:01 build returns the
        # briefing the 08:59 build already stored.
        with mock.patch("time.time",
                        return_value=_ist_epoch(2026, 11, 23, 8, 59)):
            first = briefing_mod.build_briefing(self.conn, gmail=NoGmail())
        with mock.patch("time.time",
                        return_value=_ist_epoch(2026, 11, 23, 9, 1)):
            second = briefing_mod.build_briefing(self.conn, gmail=NoGmail())
        self.assertEqual(second["id"], first["id"])

    def test_ist_day_rollover_builds_again(self):
        with mock.patch("time.time",
                        return_value=_ist_epoch(2026, 11, 23, 23, 59)):
            first = briefing_mod.build_briefing(self.conn, gmail=NoGmail())
        with mock.patch("time.time",
                        return_value=_ist_epoch(2026, 11, 24, 0, 1)):
            second = briefing_mod.build_briefing(self.conn, gmail=NoGmail())
        self.assertNotEqual(second["id"], first["id"])

    def test_guard_key_without_stored_briefing_builds(self):
        now = int(time.time())
        briefing_mod._memory_set(
            conn=self.conn, key=briefing_mod.BRIEFING_DAY_KEY,
            value=briefing_mod.ist_date(now), now=now)
        self.conn.commit()
        briefing = briefing_mod.build_briefing(self.conn, gmail=NoGmail())
        self.assertEqual(briefing["cards"][0]["kind"], "quiet")

    def test_ist_helpers(self):
        self.assertEqual(
            briefing_mod.ist_date(_ist_epoch(2026, 11, 23, 23, 59)),
            "2026-11-23")
        self.assertEqual(
            briefing_mod.ist_date(_ist_epoch(2026, 11, 24, 0, 1)),
            "2026-11-24")
        # 18:30 UTC is 00:00 IST the NEXT day — the date must
        # come from IST, never from UTC or host localtime.
        utc_midnight_edge = int(datetime(
            2026, 11, 23, 18, 30,
            tzinfo=ZoneInfo("UTC")).timestamp())
        self.assertEqual(briefing_mod.ist_date(utc_midnight_edge),
                         "2026-11-24")
        self.assertEqual(
            briefing_mod.today_briefing_ist(_ist_epoch(2026, 11, 23, 8, 0)),
            _ist_epoch(2026, 11, 23, 9, 0))
        self.assertEqual(
            briefing_mod.next_briefing_ist(_ist_epoch(2026, 11, 23, 8, 0)),
            _ist_epoch(2026, 11, 23, 9, 0))
        self.assertEqual(
            briefing_mod.next_briefing_ist(_ist_epoch(2026, 11, 23, 9, 0)),
            _ist_epoch(2026, 11, 23, 9, 0))
        self.assertEqual(
            briefing_mod.next_briefing_ist(_ist_epoch(2026, 11, 23, 10, 0)),
            _ist_epoch(2026, 11, 24, 9, 0))


class TestScopedFacts(_MissionDbCase):
    def _applied_card(self):
        cards = self._cards()
        applied = [c for c in cards if c["kind"] == "mission_applied"]
        self.assertEqual(len(applied), 1)
        return applied[0]

    def test_no_latest_line_when_fact_store_missing(self):
        # Deferred path: W5's get_scoped_summary is absent — the
        # card is built without the line, no failure. The
        # attribute is really removed here (not just unseeded),
        # so this tests the ImportError/AttributeError branch
        # whatever W5's landing state is.
        import server.memory.keyword as keyword_mod

        self._mission("m_1")
        self._activity("m_1", "applied", {"title": "SDE Intern"})
        had = hasattr(keyword_mod, "get_scoped_summary")
        saved = getattr(keyword_mod, "get_scoped_summary", None)
        if had:
            delattr(keyword_mod, "get_scoped_summary")
        try:
            card = self._applied_card()
        finally:
            if had:
                keyword_mod.get_scoped_summary = saved
        self.assertNotIn("Latest:", card["body"])

    def test_latest_line_live_with_real_fact_store(self):
        # Live path: the real get_scoped_summary reads a real
        # scoped fact (W5's scope column added here, as its
        # migration does). Skipped if W5's function is absent.
        import server.memory.keyword as keyword_mod

        if not hasattr(keyword_mod, "get_scoped_summary"):
            self.skipTest("W5 get_scoped_summary not landed yet")
        cols = {r[1] for r in
                self.conn.execute("PRAGMA table_info(facts)")}
        if "scope" not in cols:
            self.conn.execute("ALTER TABLE facts ADD COLUMN scope TEXT")
        self._mission("m_1")
        self._activity("m_1", "applied", {"title": "SDE Intern"})
        now = int(time.time())
        self.conn.execute(
            "INSERT INTO facts (id, text, source, created_at, scope)"
            " VALUES ('f_live', 'Prefers remote roles', 'test', ?, 'm_1')",
            (now,),
        )
        self.conn.commit()
        card = self._applied_card()
        self.assertIn("Latest: Prefers remote roles", card["body"])

    def test_latest_line_when_fact_store_present(self):
        self._mission("m_1")
        self._activity("m_1", "applied", {"title": "SDE Intern"})
        fake = [{"text": "Prefers remote roles"},
                {"text": "Older fact"}]
        with mock.patch("server.memory.keyword.get_scoped_summary",
                        return_value=fake, create=True):
            card = self._applied_card()
        self.assertIn("Latest: Prefers remote roles", card["body"])
        self.assertNotIn("Older fact", card["body"])

    def test_no_latest_line_for_paused_mission(self):
        # The line is for ACTIVE missions' cards only.
        self._mission("m_1", status="paused")
        self._activity("m_1", "applied", {"title": "SDE Intern"})
        with mock.patch("server.memory.keyword.get_scoped_summary",
                        return_value=[{"text": "Prefers remote roles"}],
                        create=True):
            card = self._applied_card()
        self.assertNotIn("Latest:", card["body"])

    def test_fact_store_failure_does_not_break_briefing(self):
        self._mission("m_1")
        self._activity("m_1", "applied", {"title": "SDE Intern"})
        with mock.patch("server.memory.keyword.get_scoped_summary",
                        side_effect=RuntimeError("store down"),
                        create=True):
            card = self._applied_card()
        self.assertNotIn("Latest:", card["body"])

    def test_empty_facts_no_line(self):
        self._mission("m_1")
        self._activity("m_1", "applied", {"title": "SDE Intern"})
        with mock.patch("server.memory.keyword.get_scoped_summary",
                        return_value=[], create=True):
            card = self._applied_card()
        self.assertNotIn("Latest:", card["body"])


class TestBriefingApiForce(unittest.TestCase):
    """POST /monitor/briefing accepts {"force": bool} (W3)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_dir = config.DATA_DIR
        self._old_token = config._token
        self._old_env = os.environ.get("ECLIPSE_TOKEN")
        config.DATA_DIR = Path(self.tmp.name)
        config._token = None
        os.environ["ECLIPSE_TOKEN"] = TOKEN
        db.migrate()
        self.conn = db.connect()
        # No inbox scan in API tests — the Gmail connector would
        # otherwise try a real lookup.
        self.conn.execute(
            "UPDATE connectors SET enabled = 0 WHERE name = 'gmail'")
        self.conn.commit()
        from server.main import app
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.conn.close()
        config.DATA_DIR = self._old_dir
        config._token = self._old_token
        if self._old_env is None:
            os.environ.pop("ECLIPSE_TOKEN", None)
        else:
            os.environ["ECLIPSE_TOKEN"] = self._old_env
        self.tmp.cleanup()

    def _post(self, body=None):
        headers = {"X-Eclipse-Token": TOKEN}
        if body is None:
            return self.client.post("/monitor/briefing", headers=headers)
        return self.client.post("/monitor/briefing", json=body,
                                headers=headers)

    def test_post_twice_returns_same_id(self):
        first = self._post()
        self.assertEqual(first.status_code, 200, first.text)
        second = self._post()
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second.json()["id"], first.json()["id"])

    def test_post_force_rebuilds(self):
        first = self._post()
        forced = self._post({"force": True})
        self.assertEqual(forced.status_code, 200, forced.text)
        self.assertNotEqual(forced.json()["id"], first.json()["id"])
        # And a plain post after the force still returns the
        # forced briefing (the guard key was refreshed).
        again = self._post()
        self.assertEqual(again.json()["id"], forced.json()["id"])

    def test_post_no_body_still_works(self):
        resp = self._post()
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIn("cards", resp.json())

    def test_post_requires_token(self):
        resp = self.client.post("/monitor/briefing")
        self.assertEqual(resp.status_code, 401)


if __name__ == "__main__":
    unittest.main()
