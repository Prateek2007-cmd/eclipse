"""Sprint 7 tests: connectors (Gmail / Calendar / Drive) + registry.

Run: .venv/bin/python -m unittest discover -s tests -v

The connector classes take an injectable GwsCli transport, so
these tests never touch the real mailbox, calendar or Drive.
The API test hits the live server but only calls read-only
status/list endpoints (and the disabled-connector 503 path).
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

from server.connectors import registry as conn_registry
from server.connectors.base import ConnectorError, GwsCli
from server.connectors.calendar import CalendarConnector
from server.connectors.drive import DriveConnector
from server.connectors.gmail import GmailConnector
from server.core import config, db

REPO_ROOT = Path(__file__).resolve().parents[1]
TOKEN = "test-token-connectors"


def _fake_cli(responses: dict, calls: list):
    """A GwsCli whose transport returns canned data.

    `responses` maps a command prefix (tuple of argv words) to
    the data dict to return. Every call is recorded in `calls`.
    """

    def transport(argv, timeout):
        calls.append(list(argv))
        for prefix, data in responses.items():
            if tuple(argv[: len(prefix)]) == tuple(prefix):
                return {"ok": True, "data": data, "raw": json.dumps(data)}
        raise AssertionError(f"unexpected CLI call: {argv}")

    return GwsCli(transport=transport)


class TestGwsCli(unittest.TestCase):
    def test_parses_json_output(self):
        cli = _fake_cli({("gmail", "status"): {"status": "connected"}}, [])
        self.assertEqual(cli.run("gmail", "status"), {"status": "connected"})

    def test_rate_limit_kind_becomes_quota_error(self):
        cli = _fake_cli(
            {("gmail", "+send"): {"kind": "connector_rate_limited"}}, []
        )
        with self.assertRaises(ConnectorError) as ctx:
            cli.run("gmail", "+send")
        self.assertEqual(ctx.exception.kind, "quota")

    def test_non_json_output_returns_raw(self):
        def transport(argv, timeout):
            return {"ok": True, "data": {"raw": "plain text"}, "raw": "plain text"}

        cli = GwsCli(transport=transport)
        self.assertEqual(cli.run("gmail", "status"), {"raw": "plain text"})


class TestGmailConnector(unittest.TestCase):
    def test_status_connected(self):
        cli = _fake_cli({("gmail", "status"): {"status": "connected"}}, [])
        self.assertEqual(GmailConnector(cli).status()["state"], "connected")

    def test_status_needs_reconnect_carries_url(self):
        cli = _fake_cli(
            {("gmail", "status"): {
                "status": "not_connected",
                "connect_url": "https://example.invalid/connect",
            }},
            [],
        )
        result = GmailConnector(cli).status()
        self.assertEqual(result["state"], "needs_reconnect")
        self.assertEqual(result["connect_url"], "https://example.invalid/connect")

    def test_search_offers_redacts_bodies(self):
        calls = []
        cli = _fake_cli(
            {("gmail", "+triage"): {"messages": [
                {
                    "id": "m1", "from": "hr@acme.example",
                    "subject": "Interview invite",
                    "date": "2026-11-21",
                    "body": "Hi, write to test.user@example.com to confirm.",
                },
                {
                    "id": "m2", "from": "noreply@jobs.example",
                    "subject": "Your application",
                    "date": "2026-11-21",
                    "body": "No PII here, just a status update.",
                },
            ]}},
            calls,
        )
        offers = GmailConnector(cli).search_offers()
        self.assertEqual(len(offers), 2)
        self.assertNotIn("test.user@example.com", offers[0]["body_redacted"])
        self.assertIn("[EMAIL_1]", offers[0]["body_redacted"])
        # The query names the offer keywords.
        self.assertIn("offer", calls[0][3])

    def test_send_rate_limit(self):
        # Patch the interval to 0 — 10 real sends would
        # otherwise take ~45s of sleeps.
        import server.connectors.gmail as gmail_mod

        old = gmail_mod.SEND_INTERVAL_S
        gmail_mod.SEND_INTERVAL_S = 0
        try:
            calls = []
            cli = _fake_cli({("gmail", "+send"): {"message_id": "msg_1"}}, calls)
            connector = GmailConnector(cli)
            for _ in range(10):
                connector.send("task_1", "a@example.com", "s", "b")
            with self.assertRaises(ConnectorError) as ctx:
                connector.send("task_1", "a@example.com", "s", "b")
            self.assertEqual(ctx.exception.kind, "quota")
            self.assertEqual(len(calls), 10)
        finally:
            gmail_mod.SEND_INTERVAL_S = old

    def test_check_bounce_found_and_not_found(self):
        found = _fake_cli(
            {("gmail", "+triage"): {"messages": [
                {"subject": "Delivery Status Notification (Failure)"}
            ]}},
            [],
        )
        self.assertTrue(
            GmailConnector(found).check_bounce("gone@example.com")["bounced"]
        )
        empty = _fake_cli({("gmail", "+triage"): {"messages": []}}, [])
        self.assertFalse(
            GmailConnector(empty).check_bounce("ok@example.com")["bounced"]
        )


class TestCalendarConnector(unittest.TestCase):
    def test_freebusy_returns_busy_blocks(self):
        busy = _fake_cli(
            {("calendar", "freebusy"): {"calendars": {"primary": {"busy": [
                {"start": "2026-11-22T10:00:00+05:30",
                 "end": "2026-11-22T11:00:00+05:30"}
            ]}}}},
            [],
        )
        data = CalendarConnector(busy).freebusy(
            "2026-11-22T09:00:00+05:30", "2026-11-22T12:00:00+05:30")
        self.assertEqual(len(data["calendars"]["primary"]["busy"]), 1)
        free = _fake_cli(
            {("calendar", "freebusy"): {"calendars": {"primary": {"busy": []}}}},
            [],
        )
        data = CalendarConnector(free).freebusy(
            "2026-11-22T09:00:00+05:30", "2026-11-22T10:00:00+05:30")
        self.assertEqual(data["calendars"]["primary"]["busy"], [])

    def test_freebusy_returns_raw_dict_unchanged(self):
        cli = _fake_cli({("calendar", "freebusy"): {"raw": "garbage"}}, [])
        # {"raw": ...} is a dict, so freebusy returns it — the
        # API layer is what fails closed (see TestConnectorsApiUnit).
        data = CalendarConnector(cli).freebusy(
            "2026-11-22T09:00:00+05:30", "2026-11-22T10:00:00+05:30")
        self.assertEqual(data, {"raw": "garbage"})

    def test_create_event_no_id_raises(self):
        cli = _fake_cli({("calendar", "events", "insert"): {}}, [])
        with self.assertRaises(ConnectorError):
            CalendarConnector(cli).create_event(
                "Interview", "2026-11-22T10:00:00+05:30",
                "2026-11-22T11:00:00+05:30",
            )

    def test_create_event_normalises_times(self):
        calls = []
        cli = _fake_cli(
            {("calendar", "events", "insert"): {
                "id": "ev_1", "summary": "Interview",
                "start": {"dateTime": "2026-11-22T10:00:00+05:30"},
                "end": {"dateTime": "2026-11-22T11:00:00+05:30"},
                "htmlLink": "https://calendar.example/ev_1",
            }},
            calls,
        )
        event = CalendarConnector(cli).create_event(
            "Interview", "2026-11-22T10:00:00+05:30",
            "2026-11-22T11:00:00+05:30",
            description="Acme Tech SDE intern",
        )
        self.assertEqual(event["id"], "ev_1")
        self.assertEqual(event["htmlLink"], "https://calendar.example/ev_1")
        body = json.loads(calls[0][calls[0].index("--json") + 1])
        self.assertEqual(body["summary"], "Interview")
        self.assertEqual(body["description"], "Acme Tech SDE intern")


class TestDriveConnector(unittest.TestCase):
    def test_find_resume_picks_newest(self):
        cli = _fake_cli(
            {("drive", "files", "list"): {"files": [
                {"id": "f1", "name": "resume-old.pdf",
                 "modifiedTime": "2025-01-01T00:00:00Z"},
                {"id": "f2", "name": "resume.pdf",
                 "modifiedTime": "2026-11-01T00:00:00Z"},
            ]}},
            [],
        )
        resume = DriveConnector(cli).find_resume()
        self.assertEqual(resume["id"], "f2")

    def test_find_resume_none_when_empty(self):
        cli = _fake_cli({("drive", "files", "list"): {"files": []}}, [])
        self.assertIsNone(DriveConnector(cli).find_resume())


class TestRegistry(unittest.TestCase):
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

    def test_all_three_seeded_and_on(self):
        rows = conn_registry.get_all(self.conn)
        self.assertEqual({r["name"] for r in rows}, {"gmail", "calendar", "drive"})
        self.assertTrue(all(r["enabled"] for r in rows))

    def test_disable_and_reenable(self):
        row = conn_registry.set_enabled(self.conn, "gmail", False)
        self.assertFalse(row["enabled"])
        self.assertFalse(conn_registry.get(self.conn, "gmail")["enabled"])
        conn_registry.set_enabled(self.conn, "gmail", True)
        self.assertTrue(conn_registry.get(self.conn, "gmail")["enabled"])

    def test_unknown_name_is_none(self):
        self.assertIsNone(conn_registry.get(self.conn, "slack"))
        self.assertIsNone(conn_registry.set_enabled(self.conn, "slack", False))

    def test_record_status(self):
        conn_registry.record_status(self.conn, "gmail", "connected")
        row = conn_registry.get(self.conn, "gmail")
        self.assertEqual(row["state"], "connected")
        self.assertIsNotNone(row["last_checked"])


class TestBounceCheck(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old = config.DATA_DIR
        config.DATA_DIR = Path(self.tmp.name)
        db.migrate()
        self.conn = db.connect()
        from server.tasks import loop

        self.task_id = loop.create_task(self.conn, "Cold-mail recruiters")
        from server.jobs import profile
        from server.mail import drafts as draft_mod

        self.draft_id = draft_mod.create_draft(
            self.conn, self.task_id, "gone@example.com",
            {"id": "role_1", "title": "SDE Intern", "company": "Acme"},
            profile.TEST_PROFILE, [],
        )
        draft_mod.approve_draft(self.conn, self.draft_id)
        from server.mail import send as send_mod

        send_mod.send_draft(self.conn, self.draft_id, transport=send_mod.mock_transport)

    def tearDown(self):
        self.conn.close()
        config.DATA_DIR = self._old
        self.tmp.cleanup()

    def test_bounce_updates_log_and_draft(self):
        from server.connectors.gmail import GmailConnector
        from server.mail import send as send_mod

        calls = []

        class FakeGmail(GmailConnector):
            def check_bounce(self, recipient):
                calls.append(recipient)
                return {"bounced": True, "detail": "Delivery Status Notification"}

        results = send_mod.check_bounces_for_task(
            self.conn, self.task_id, gmail=FakeGmail()
        )
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["bounced"])
        log = send_mod.mail_log(self.conn, self.task_id)
        self.assertEqual(log[0]["status"], "bounced")
        from server.mail import drafts as draft_mod

        self.assertEqual(
            draft_mod.get_draft(self.conn, self.draft_id)["status"], "bounced"
        )

    def test_no_bounce_leaves_log_alone(self):
        from server.connectors.gmail import GmailConnector
        from server.mail import send as send_mod

        class FakeGmail(GmailConnector):
            def check_bounce(self, recipient):
                return {"bounced": False, "detail": ""}

        results = send_mod.check_bounces_for_task(
            self.conn, self.task_id, gmail=FakeGmail()
        )
        self.assertFalse(results[0]["bounced"])
        log = send_mod.mail_log(self.conn, self.task_id)
        self.assertEqual(log[0]["status"], "sent")


class TestFindResume(unittest.TestCase):
    def test_newer_decoy_does_not_win(self):
        # B1: the folder holds other people's documents. A newer
        # ServiceNow file that is not the user's must never be
        # attached to a recruiter mail.
        import os
        from server.api import mail_api

        with tempfile.TemporaryDirectory() as tmp:
            user_dir = Path(tmp)
            akash = user_dir / "Akash_Goud_Sidduluri_ServiceNow.pdf"
            akash.write_bytes(b"%PDF-1.4 fake resume")
            os.utime(akash, (1000000000, 1000000000))
            harika = user_dir / (
                "Harika_Narla_ServiceNow_Summer_Internship_"
                "Interview_Experience.pdf"
            )
            harika.write_bytes(b"%PDF-1.4 someone else")
            os.utime(harika, (1700000000, 1700000000))
            found = mail_api._find_resume(user_files=user_dir)
            self.assertEqual(found, str(akash))

    def test_no_match_returns_none(self):
        from server.api import mail_api

        with tempfile.TemporaryDirectory() as tmp:
            user_dir = Path(tmp)
            (user_dir / "Someone_Else_Notes.pdf").write_bytes(b"x")
            self.assertIsNone(mail_api._find_resume(user_files=user_dir))

    def test_attach_defaults_to_opt_in(self):
        from server.api.mail_api import SendIn

        self.assertFalse(SendIn().attach_resume)


class TestConnectorsApiUnit(unittest.TestCase):
    """The API layer with fake connector classes (in-process,
    no live Google calls)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old = config.DATA_DIR
        config.DATA_DIR = Path(self.tmp.name)
        db.migrate()
        self.conn = db.connect()
        from server.tasks import loop

        self.task_id = loop.create_task(self.conn, "Interview prep")
        from server.api import connectors_api

        self.api = connectors_api
        self._orig_gmail = connectors_api.GmailConnector
        self._orig_calendar = connectors_api.CalendarConnector
        self._orig_drive = connectors_api.DriveConnector

    def tearDown(self):
        self.api.GmailConnector = self._orig_gmail
        self.api.CalendarConnector = self._orig_calendar
        self.api.DriveConnector = self._orig_drive
        self.conn.close()
        config.DATA_DIR = self._old
        self.tmp.cleanup()

    def _approve(self, kind, **detail):
        body = self.api.ApprovalIn(task_id=self.task_id, kind=kind, **detail)
        result = self.api.create_approval(body, _token="test")
        return result["approval_id"]

    def test_freebusy_fail_closed_on_malformed_output(self):
        class FakeCalendar:
            def freebusy(self, a, b):
                return {"raw": "garbage"}

        self.api.CalendarConnector = FakeCalendar
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as ctx:
            self.api.calendar_freebusy(
                "2026-11-22T09:00:00+05:30", "2026-11-22T10:00:00+05:30",
                _token="test",
            )
        self.assertEqual(ctx.exception.status_code, 502)

    def test_freebusy_returns_busy_list_not_raw(self):
        class FakeCalendar:
            def freebusy(self, a, b):
                return {"calendars": {"primary": {"busy": [
                    {"start": "2026-11-22T10:00:00+05:30",
                     "end": "2026-11-22T11:00:00+05:30"}
                ]}}}

        self.api.CalendarConnector = FakeCalendar
        result = self.api.calendar_freebusy(
            "2026-11-22T09:00:00+05:30", "2026-11-22T12:00:00+05:30",
            _token="test",
        )
        self.assertFalse(result["free"])
        self.assertEqual(len(result["busy"]), 1)
        self.assertNotIn("raw", result)

    def test_create_event_needs_approval(self):
        from fastapi import HTTPException

        body = self.api.EventIn(
            task_id=self.task_id, approval_id="appr_missing",
            summary="Interview", start="2026-11-22T10:00:00+05:30",
            end="2026-11-22T11:00:00+05:30",
        )
        with self.assertRaises(HTTPException) as ctx:
            self.api.create_event(body, _token="test")
        self.assertEqual(ctx.exception.status_code, 409)

    def test_create_event_consumes_approval_once(self):
        calls = []

        class FakeCalendar:
            def create_event(self, *a, **k):
                calls.append(a)
                return {
                    "id": "ev_1", "summary": a[0], "start": a[1],
                    "end": a[2], "htmlLink": None,
                }

        self.api.CalendarConnector = FakeCalendar
        approval = self._approve(
            "calendar_event", summary="Interview",
            start="2026-11-22T10:00:00+05:30",
        )
        body = self.api.EventIn(
            task_id=self.task_id, approval_id=approval,
            summary="Interview", start="2026-11-22T10:00:00+05:30",
            end="2026-11-22T11:00:00+05:30",
        )
        event = self.api.create_event(body, _token="test")
        self.assertEqual(event["id"], "ev_1")
        # The same approval cannot be used again.
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as ctx:
            self.api.create_event(body, _token="test")
        self.assertEqual(ctx.exception.status_code, 409)

    def test_create_event_duplicate_returns_existing(self):
        calls = []

        class FakeCalendar:
            def create_event(self, *a, **k):
                calls.append(a)
                return {
                    "id": "ev_1", "summary": a[0], "start": a[1],
                    "end": a[2], "htmlLink": None,
                }

        self.api.CalendarConnector = FakeCalendar
        first = self._approve(
            "calendar_event", summary="Interview",
            start="2026-11-22T10:00:00+05:30",
        )
        body = self.api.EventIn(
            task_id=self.task_id, approval_id=first,
            summary="Interview", start="2026-11-22T10:00:00+05:30",
            end="2026-11-22T11:00:00+05:30",
        )
        self.api.create_event(body, _token="test")
        second = self._approve(
            "calendar_event", summary="Interview",
            start="2026-11-22T10:00:00+05:30",
        )
        body2 = self.api.EventIn(
            task_id=self.task_id, approval_id=second,
            summary="Interview", start="2026-11-22T10:00:00+05:30",
            end="2026-11-22T11:00:00+05:30",
        )
        dup = self.api.create_event(body2, _token="test")
        self.assertTrue(dup.get("duplicate"))
        self.assertEqual(dup["id"], "ev_1")
        self.assertEqual(len(calls), 1)

    def test_create_event_failure_keeps_approval(self):
        class FakeCalendar:
            def create_event(self, *a, **k):
                raise ConnectorError("calendar", "boom", "error")

        self.api.CalendarConnector = FakeCalendar
        approval = self._approve(
            "calendar_event", summary="Interview",
            start="2026-11-22T10:00:00+05:30",
        )
        body = self.api.EventIn(
            task_id=self.task_id, approval_id=approval,
            summary="Interview", start="2026-11-22T10:00:00+05:30",
            end="2026-11-22T11:00:00+05:30",
        )
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as ctx:
            self.api.create_event(body, _token="test")
        self.assertEqual(ctx.exception.status_code, 502)
        # The approval survives the failure — a retry works.
        row = self.conn.execute(
            "SELECT COUNT(*) AS c FROM grants WHERE id = ?", (approval,)
        ).fetchone()
        self.assertEqual(row["c"], 1)

    def test_approval_wrong_kind_rejected(self):
        from fastapi import HTTPException

        approval = self._approve("drive_upload", path="/home/hatch/x.pdf")
        body = self.api.EventIn(
            task_id=self.task_id, approval_id=approval,
            summary="Interview", start="2026-11-22T10:00:00+05:30",
            end="2026-11-22T11:00:00+05:30",
        )
        with self.assertRaises(HTTPException) as ctx:
            self.api.create_event(body, _token="test")
        self.assertEqual(ctx.exception.status_code, 409)

    def test_drive_upload_path_validation(self):
        from fastapi import HTTPException

        for bad in ("relative/path.pdf", "/etc/passwd", "-rf", "/tmp/x.pdf"):
            with self.assertRaises(HTTPException) as ctx:
                self.api._validate_upload_path(bad)
            self.assertEqual(ctx.exception.status_code, 422)
        home_file = str(Path.home() / "receipt.pdf")
        self.assertEqual(self.api._validate_upload_path(home_file), home_file)

    def test_event_rejects_bad_timestamps(self):
        from pydantic import ValidationError

        with self.assertRaises(ValidationError):
            self.api.EventIn(
                task_id=self.task_id, approval_id="x",
                summary="Interview", start="tomorrow",
                end="2026-11-22T11:00:00+05:30",
            )
        with self.assertRaises(ValidationError):
            self.api.EventIn(
                task_id=self.task_id, approval_id="x",
                summary="Interview", start="2026-11-22T11:00:00+05:30",
                end="2026-11-22T10:00:00+05:30",
            )


def _free_port() -> int:
    with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _request(base_url: str, method: str, path: str, token: str | None = TOKEN):
    req = _urlrequest.Request(f"{base_url}{path}", method=method)
    if token:
        req.add_header("X-Eclipse-Token", token)
    try:
        with _urlrequest.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())
    except _urlerror.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())
    except _urlerror.URLError:
        return 0, {}


class TestConnectorsApi(unittest.TestCase):
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
            raise RuntimeError("server did not come up for connectors API test")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=10)
        except _subprocess.TimeoutExpired:
            cls.proc.kill()
        cls.tmp.cleanup()

    def test_list_returns_three(self):
        status, body = _request(self.base, "GET", "/connectors")
        self.assertEqual(status, 200, body)
        names = {c["name"] for c in body["connectors"]}
        self.assertEqual(names, {"gmail", "calendar", "drive"})

    def test_requires_token(self):
        status, _ = _request(self.base, "GET", "/connectors", token=None)
        self.assertEqual(status, 401)

    def test_disable_blocks_offers(self):
        status, _ = _request(self.base, "POST", "/connectors/gmail/disable")
        self.assertEqual(status, 200)
        status, body = _request(self.base, "GET", "/connectors/gmail/offers")
        self.assertEqual(status, 503, body)
        status, _ = _request(self.base, "POST", "/connectors/gmail/enable")
        self.assertEqual(status, 200)

    def test_live_status_is_connected(self):
        # Read-only live check against the platform CLI.
        status, body = _request(self.base, "GET", "/connectors/gmail/status")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["state"], "connected")

    def test_unknown_connector_404(self):
        status, _ = _request(self.base, "GET", "/connectors/slack/status")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
