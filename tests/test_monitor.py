"""Sprint 8 tests: live monitoring (watches + briefing).

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
import urllib.error as _urlerror
import urllib.request as _urlrequest
from pathlib import Path

from server.core import config, db
from server.monitor import briefing as briefing_mod
from server.monitor import watches as watch_mod
from server.tasks import loop

REPO_ROOT = Path(__file__).resolve().parents[1]
TOKEN = "test-token-monitor"


class TestWatches(unittest.TestCase):
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

    def _watch(self, condition="changed", url="http://localhost/result.html"):
        return watch_mod.create_watch(
            self.conn, url, "#result_status", condition, interval_min=15
        )

    def test_interval_minimum_is_15(self):
        with self.assertRaises(ValueError):
            watch_mod.create_watch(
                self.conn, "http://localhost/x", "#s", "changed",
                interval_min=5,
            )

    def test_unknown_condition_rejected(self):
        with self.assertRaises(ValueError):
            watch_mod.create_watch(
                self.conn, "http://localhost/x", "#s", "watched",
                interval_min=15,
            )

    def test_contains_needs_text(self):
        with self.assertRaises(ValueError):
            watch_mod.create_watch(
                self.conn, "http://localhost/x", "#s", "contains:",
                interval_min=15,
            )

    def test_first_sighting_is_the_baseline(self):
        w = self._watch()
        result = watch_mod.check_watch(self.conn, w["id"], True, "Not selected")
        self.assertFalse(result["fired"])
        self.assertEqual(result["reason"], "baseline recorded")
        self.assertEqual(
            watch_mod.get_watch(self.conn, w["id"])["last_value"], "Not selected"
        )

    def test_changed_fires_once_per_value(self):
        w = self._watch()
        watch_mod.check_watch(self.conn, w["id"], True, "Not selected")
        result = watch_mod.check_watch(self.conn, w["id"], True, "Selected")
        self.assertTrue(result["fired"])
        # Same value again: no fire.
        result = watch_mod.check_watch(self.conn, w["id"], True, "Selected")
        self.assertFalse(result["fired"])
        self.assertEqual(result["reason"], "no change")

    def test_changed_fires_again_on_a_new_value_inside_cooldown(self):
        w = self._watch()
        watch_mod.check_watch(self.conn, w["id"], True, "A")
        watch_mod.check_watch(self.conn, w["id"], True, "B")
        result = watch_mod.check_watch(self.conn, w["id"], True, "C")
        self.assertTrue(result["fired"])

    def test_appeared_fires_on_first_sighting(self):
        w = self._watch(condition="appeared")
        result = watch_mod.check_watch(self.conn, w["id"], True, "Offer letter")
        self.assertTrue(result["fired"])

    def test_disappeared_fires_when_the_element_goes(self):
        w = self._watch(condition="disappeared")
        watch_mod.check_watch(self.conn, w["id"], True, "Apply now")
        result = watch_mod.check_watch(self.conn, w["id"], False, None)
        self.assertTrue(result["fired"])
        self.assertEqual(watch_mod.get_watch(self.conn, w["id"])["last_value"], None)

    def test_contains_fires_then_cools_down(self):
        w = self._watch(condition="contains:Selected")
        # Baseline: no fire.
        result = watch_mod.check_watch(self.conn, w["id"], True, "Result: Selected")
        self.assertFalse(result["fired"])
        # First time the condition is true after the baseline: fires.
        result = watch_mod.check_watch(self.conn, w["id"], True, "Result: Selected")
        self.assertTrue(result["fired"])
        # Same value again inside the hour: suppressed by cooldown.
        result = watch_mod.check_watch(self.conn, w["id"], True, "Result: Selected")
        self.assertFalse(result["fired"])
        self.assertIn("cooldown", result["reason"])

    def test_three_misses_break_the_watch(self):
        w = self._watch()
        watch_mod.check_watch(self.conn, w["id"], True, "Not selected")
        for i in (1, 2):
            result = watch_mod.check_watch(self.conn, w["id"], False, None)
            self.assertFalse(result["broken"])
            self.assertIn(f"{i}/3", result["reason"])
        result = watch_mod.check_watch(self.conn, w["id"], False, None)
        self.assertTrue(result["broken"])
        self.assertEqual(watch_mod.get_watch(self.conn, w["id"])["status"], "broken")
        events = watch_mod.list_events(self.conn, w["id"])
        self.assertEqual(events[0]["kind"], "broken")

    def test_a_hit_resets_the_miss_counter(self):
        w = self._watch()
        watch_mod.check_watch(self.conn, w["id"], True, "Not selected")
        watch_mod.check_watch(self.conn, w["id"], False, None)
        watch_mod.check_watch(self.conn, w["id"], False, None)
        watch_mod.check_watch(self.conn, w["id"], True, "Not selected")
        self.assertEqual(watch_mod.get_watch(self.conn, w["id"])["misses"], 0)

    def test_mute_and_unmute(self):
        w = self._watch()
        muted = watch_mod.mute_watch(self.conn, w["id"], 30)
        self.assertEqual(muted["status"], "muted")
        self.assertIsNotNone(muted["muted_until"])
        self.assertEqual(len(watch_mod.due_watches(self.conn)), 0)
        unmuted = watch_mod.unmute_watch(self.conn, w["id"])
        self.assertEqual(unmuted["status"], "active")

    def test_due_only_when_interval_passed(self):
        w = self._watch()
        # Never checked: due.
        self.assertEqual(len(watch_mod.due_watches(self.conn)), 1)
        watch_mod.check_watch(self.conn, w["id"], True, "Not selected")
        # Just checked: not due for 15 minutes.
        self.assertEqual(len(watch_mod.due_watches(self.conn)), 0)

    def test_delete_removes_watch_and_events(self):
        w = self._watch()
        watch_mod.check_watch(self.conn, w["id"], True, "A")
        watch_mod.check_watch(self.conn, w["id"], True, "B")
        self.assertTrue(watch_mod.delete_watch(self.conn, w["id"]))
        self.assertIsNone(watch_mod.get_watch(self.conn, w["id"]))
        self.assertEqual(watch_mod.list_events(self.conn, w["id"]), [])

    def test_contains_new_value_fires_inside_cooldown(self):
        # B1: the cooldown suppresses the SAME change, not a new
        # one — "Selected" then "Selected — 2 offers" 10 minutes
        # later must alert.
        w = self._watch(condition="contains:Selected")
        watch_mod.check_watch(self.conn, w["id"], True, "Result: Selected")
        watch_mod.check_watch(self.conn, w["id"], True, "Result: Selected")
        result = watch_mod.check_watch(
            self.conn, w["id"], True, "Result: Selected — 2 offers")
        self.assertTrue(result["fired"])

    def test_expired_mute_is_due_again(self):
        # B2: a mute is a pause, not a permanent stop.
        w = self._watch()
        watch_mod.mute_watch(self.conn, w["id"], 1)
        self.assertEqual(len(watch_mod.due_watches(self.conn)), 0)
        # Backdate the mute past its expiry.
        self.conn.execute(
            "UPDATE watches SET muted_until = ? WHERE id = ?",
            (watch_mod._now() - 60, w["id"]),
        )
        self.conn.commit()
        self.assertEqual(len(watch_mod.due_watches(self.conn)), 1)

    def test_broken_watch_recovers_on_a_good_read(self):
        # B6: a broken watch whose element is back heals.
        w = self._watch()
        watch_mod.check_watch(self.conn, w["id"], True, "Not selected")
        for _ in range(3):
            watch_mod.check_watch(self.conn, w["id"], False, None)
        self.assertEqual(watch_mod.get_watch(self.conn, w["id"])["status"], "broken")
        result = watch_mod.check_watch(self.conn, w["id"], True, "Not selected")
        self.assertEqual(result["status"], "active")
        self.assertIn("recovered", result["reason"])
        self.assertEqual(watch_mod.get_watch(self.conn, w["id"])["status"], "active")

    def test_disappeared_watch_does_not_go_broken_after_firing(self):
        # M2: a watch that did its job must not break on the
        # steady-state absence that follows.
        w = self._watch(condition="disappeared")
        watch_mod.check_watch(self.conn, w["id"], True, "Apply now")
        watch_mod.check_watch(self.conn, w["id"], False, None)
        for _ in range(3):
            result = watch_mod.check_watch(self.conn, w["id"], False, None)
            self.assertFalse(result["broken"])
        self.assertEqual(watch_mod.get_watch(self.conn, w["id"])["status"], "active")

    def test_muted_watch_does_not_fire(self):
        # M8: mute is enforced by the check itself.
        w = self._watch()
        watch_mod.check_watch(self.conn, w["id"], True, "A")
        watch_mod.mute_watch(self.conn, w["id"], 60)
        result = watch_mod.check_watch(self.conn, w["id"], True, "B")
        self.assertFalse(result["fired"])
        self.assertEqual(result["reason"], "muted")

    def test_mute_does_not_hide_a_broken_watch(self):
        # N3: muting a broken watch leaves it broken.
        w = self._watch()
        for _ in range(3):
            watch_mod.check_watch(self.conn, w["id"], False, None)
        watch_mod.mute_watch(self.conn, w["id"], 60)
        self.assertEqual(watch_mod.get_watch(self.conn, w["id"])["status"], "broken")

    def test_repair_restarts_from_a_fresh_baseline(self):
        w = self._watch()
        for _ in range(3):
            watch_mod.check_watch(self.conn, w["id"], False, None)
        repaired = watch_mod.repair_watch(
            self.conn, w["id"], selector="#result_status_new")
        self.assertEqual(repaired["status"], "active")
        self.assertEqual(repaired["misses"], 0)
        self.assertIsNone(repaired["last_value"])
        self.assertEqual(repaired["selector"], "#result_status_new")

    def test_events_store_redacted_values(self):
        # M11: a watched value can hold PII — events keep a
        # redacted preview, not the raw text.
        w = self._watch()
        watch_mod.check_watch(
            self.conn, w["id"], True, "Contact test.user@example.com")
        watch_mod.check_watch(
            self.conn, w["id"], True, "Contact other@example.com")
        events = watch_mod.list_events(self.conn, w["id"])
        self.assertEqual(len(events), 1)
        self.assertNotIn("test.user@example.com", events[0]["detail"])
        self.assertNotIn("other@example.com", events[0]["detail"])
        self.assertIn("[EMAIL_1]", events[0]["detail"])

    def test_events_limit_is_clamped(self):
        # M9: limit=-1 must not dump the whole table.
        w = self._watch()
        watch_mod.check_watch(self.conn, w["id"], True, "A")
        watch_mod.check_watch(self.conn, w["id"], True, "B")
        events = watch_mod.list_events(self.conn, limit=-1)
        self.assertLessEqual(len(events), 200)

    def test_appeared_does_not_fire_on_an_empty_element(self):
        # N5: an empty element is not an appearance.
        w = self._watch(condition="appeared")
        result = watch_mod.check_watch(self.conn, w["id"], True, "")
        self.assertFalse(result["fired"])

    def test_scanned_doc_page_reports_a_miss_not_a_crash(self):
        # Sprint 8 polish: an image-only page has no DOM text for
        # the selector. The check records a miss; the watch only
        # breaks after 3 in a row.
        w = watch_mod.create_watch(
            self.conn, "http://localhost/scanned-doc.html",
            "#scanned_text", "changed", interval_min=15,
        )
        result = watch_mod.check_watch(self.conn, w["id"], False, None)
        self.assertFalse(result["fired"])
        self.assertFalse(result["broken"])
        self.assertEqual(watch_mod.get_watch(self.conn, w["id"])["status"], "active")


class TestBriefing(unittest.TestCase):
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

    def test_briefing_from_firings_only(self):
        w = watch_mod.create_watch(
            self.conn, "http://localhost/result.html", "#result_status",
            "changed", interval_min=15,
        )
        watch_mod.check_watch(self.conn, w["id"], True, "Not selected")
        watch_mod.check_watch(self.conn, w["id"], True, "Selected")

        class NoGmail:
            def search_offers(self, n):
                return []

        briefing = briefing_mod.build_briefing(self.conn, gmail=NoGmail())
        self.assertEqual(len(briefing["cards"]), 1)
        self.assertEqual(briefing["cards"][0]["kind"], "watch")
        self.assertIn("Selected", briefing["cards"][0]["body"])
        latest = briefing_mod.latest_briefing(self.conn)
        self.assertEqual(latest["id"], briefing["id"])

    def test_briefing_adds_offers_and_caps_at_ten(self):
        class FakeGmail:
            def search_offers(self, n):
                return [
                    {
                        "id": f"m{i}", "subject": f"Offer {i}",
                        "body_redacted": "shortlisted",
                    }
                    for i in range(20)
                ]

        briefing = briefing_mod.build_briefing(self.conn, gmail=FakeGmail())
        self.assertEqual(len(briefing["cards"]), 10)
        self.assertTrue(all(c["kind"] == "offer" for c in briefing["cards"]))

    def test_briefing_survives_a_dead_gmail(self):
        class DeadGmail:
            def search_offers(self, n):
                raise RuntimeError("gmail down")

        briefing = briefing_mod.build_briefing(self.conn, gmail=DeadGmail())
        # N4: the briefing says the inbox scan failed instead of
        # silently shipping without it.
        self.assertEqual(len(briefing["cards"]), 1)
        self.assertEqual(briefing["cards"][0]["kind"], "inbox_unavailable")

    def test_briefing_dedups_repeat_firings_of_one_watch(self):
        # N7: one chatty watch must not fill all 10 cards.
        w = watch_mod.create_watch(
            self.conn, "http://localhost/result.html", "#result_status",
            "changed", interval_min=15,
        )
        watch_mod.check_watch(self.conn, w["id"], True, "A")
        watch_mod.check_watch(self.conn, w["id"], True, "B")
        watch_mod.check_watch(self.conn, w["id"], True, "C")

        class NoGmail:
            def search_offers(self, n):
                return []

        briefing = briefing_mod.build_briefing(self.conn, gmail=NoGmail())
        watch_cards = [c for c in briefing["cards"] if c["kind"] == "watch"]
        self.assertEqual(len(watch_cards), 1)
        self.assertIn("C", watch_cards[0]["body"])

    def test_broken_watch_gets_a_card(self):
        w = watch_mod.create_watch(
            self.conn, "http://localhost/result.html", "#gone",
            "changed", interval_min=15,
        )
        for _ in range(3):
            watch_mod.check_watch(self.conn, w["id"], False, None)

        class NoGmail:
            def search_offers(self, n):
                return []

        briefing = briefing_mod.build_briefing(self.conn, gmail=NoGmail())
        self.assertEqual(len(briefing["cards"]), 1)
        self.assertEqual(briefing["cards"][0]["kind"], "watch_broken")


def _free_port() -> int:
    with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _request(base_url: str, method: str, path: str, body=None,
             token: str | None = TOKEN):
    data = json.dumps(body).encode() if body is not None else None
    req = _urlrequest.Request(f"{base_url}{path}", data=data, method=method)
    if token:
        req.add_header("X-Eclipse-Token", token)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with _urlrequest.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())
    except _urlerror.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())
    except _urlerror.URLError:
        return 0, {}


class TestMonitorApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.port = _free_port()
        cls.base = f"http://127.0.0.1:{cls.port}"
        env = dict(os.environ)
        env["ECLIPSE_DATA_DIR"] = cls.tmp.name
        env["ECLIPSE_TOKEN"] = TOKEN
        env["ECLIPSE_MAIL_TRANSPORT"] = "mock"
        env.pop("ANTSEED_API_KEY", None)
        env.pop("GROQ_API_KEY", None)
        env["OLLAMA_BASE_URL"] = "http://127.0.0.1:1/v1"
        venv_python = REPO_ROOT / ".venv" / "bin" / "python"
        python = str(venv_python) if venv_python.exists() else _sys.executable
        cls.proc = _subprocess.Popen(
            [python, "-m", "uvicorn", "server.main:app",
             "--host", "127.0.0.1", "--port", str(cls.port)],
            cwd=str(REPO_ROOT), env=env,
            stdout=_subprocess.DEVNULL, stderr=_subprocess.DEVNULL,
        )
        deadline = time.time() + 20
        while time.time() < deadline:
            status, _ = _request(cls.base, "GET", "/health")
            if status == 200:
                break
            time.sleep(0.3)
        else:
            raise RuntimeError("server did not come up for monitor API test")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=10)
        except _subprocess.TimeoutExpired:
            cls.proc.kill()
        cls.tmp.cleanup()

    def test_create_and_check_flow(self):
        status, body = _request(self.base, "POST", "/monitor/watches", {
            "url": "http://localhost/result.html",
            "selector": "#result_status",
            "condition": "changed",
            "interval_min": 15,
        })
        self.assertEqual(status, 200, body)
        watch_id = body["id"]
        # Baseline.
        status, body = _request(
            self.base, "POST", f"/monitor/watches/{watch_id}/check",
            {"found": True, "value": "Not selected"},
        )
        self.assertEqual(status, 200, body)
        self.assertFalse(body["fired"])
        # Change fires + writes an audit entry.
        status, body = _request(
            self.base, "POST", f"/monitor/watches/{watch_id}/check",
            {"found": True, "value": "Selected"},
        )
        self.assertEqual(status, 200, body)
        self.assertTrue(body["fired"])
        status, body = _request(self.base, "GET", "/audit")
        self.assertEqual(status, 200, body)
        actions = [e["action"] for e in body["entries"]]
        self.assertIn("monitor.watch_fired", actions)

    def test_interval_below_15_rejected(self):
        status, body = _request(self.base, "POST", "/monitor/watches", {
            "url": "http://localhost/x", "selector": "#s",
            "condition": "changed", "interval_min": 5,
        })
        self.assertEqual(status, 422, body)

    def test_requires_token(self):
        status, _ = _request(self.base, "GET", "/monitor/watches", token=None)
        self.assertEqual(status, 401)

    def test_unknown_watch_404(self):
        status, _ = _request(self.base, "GET", "/monitor/watches/w_nope")
        self.assertEqual(status, 404)

    def test_bad_url_rejected(self):
        status, body = _request(self.base, "POST", "/monitor/watches", {
            "url": "not a url", "selector": "#s",
            "condition": "changed", "interval_min": 15,
        })
        self.assertEqual(status, 422, body)
        status, body = _request(self.base, "POST", "/monitor/watches", {
            "url": "ftp://example.com/x", "selector": "#s",
            "condition": "changed", "interval_min": 15,
        })
        self.assertEqual(status, 422, body)

    def test_bad_status_rejected(self):
        status, body = _request(
            self.base, "GET", "/monitor/watches?status=nope")
        self.assertEqual(status, 422, body)

    def test_found_without_value_rejected(self):
        status, body = _request(self.base, "POST", "/monitor/watches", {
            "url": "http://localhost/x", "selector": "#s",
            "condition": "changed", "interval_min": 15,
        })
        watch_id = body["id"]
        status, body = _request(
            self.base, "POST", f"/monitor/watches/{watch_id}/check",
            {"found": True, "value": None},
        )
        self.assertEqual(status, 422, body)

    def test_repair_endpoint(self):
        status, body = _request(self.base, "POST", "/monitor/watches", {
            "url": "http://localhost/x", "selector": "#old",
            "condition": "changed", "interval_min": 15,
        })
        watch_id = body["id"]
        for _ in range(3):
            _request(
                self.base, "POST", f"/monitor/watches/{watch_id}/check",
                {"found": False, "value": None},
            )
        status, body = _request(
            self.base, "GET", f"/monitor/watches/{watch_id}")
        self.assertEqual(body["status"], "broken")
        status, body = _request(
            self.base, "PATCH", f"/monitor/watches/{watch_id}",
            {"selector": "#new"},
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["status"], "active")
        self.assertEqual(body["selector"], "#new")

    def test_broken_audit_logged_once(self):
        # M5: repeat misses on an already-broken watch must not
        # spam the audit chain. (The audit stores hash-only
        # payloads, so count before/after instead of matching a
        # watch id.)
        status, body = _request(self.base, "GET", "/audit")
        before = len([e for e in body["entries"]
                      if e["action"] == "monitor.watch_broken"])
        status, body = _request(self.base, "POST", "/monitor/watches", {
            "url": "http://localhost/x", "selector": "#s",
            "condition": "changed", "interval_min": 15,
        })
        watch_id = body["id"]
        for _ in range(4):
            _request(
                self.base, "POST", f"/monitor/watches/{watch_id}/check",
                {"found": False, "value": None},
            )
        status, body = _request(self.base, "GET", "/audit")
        self.assertEqual(status, 200, body)
        after = len([e for e in body["entries"]
                     if e["action"] == "monitor.watch_broken"])
        self.assertEqual(after - before, 1, body["entries"])


if __name__ == "__main__":
    unittest.main()
