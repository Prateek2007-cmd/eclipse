"""Sprint 4 tests: profile parsing, role matching (the 12 fixture
roles: 5 match / 7 do not), receipts, already-applied checks, and
approval-before-submit — plus the end-to-end job-hunt flow on the
fixtures (match → shortlist → apply with approval → receipt).

Run: .venv/bin/python -m unittest discover -s tests -v
"""
from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
import os
import re
import shutil
import tempfile
import unittest

from server.core import config, db
from server.jobs import applied, matcher, profile, receipts
from server.planner import validate
from server.tasks import loop

TODAY = _dt.date.today()


def _role(role_id, title, company, stipend, location, days, **kw):
    role = {
        "id": role_id, "title": title, "company": company,
        "site": "testjobs", "url": f"http://localhost/jobs/{role_id}",
        "stipend": stipend, "location": location,
        "deadline": str(TODAY + _dt.timedelta(days=days)),
        "skills": ["Python", "JavaScript"], "min_year": 3,
        "min_cgpa": None, "requires_experience": False,
    }
    role.update(kw)
    return role


def _parse_fixture_roles() -> list[dict]:
    """Read the 12 roles straight from the job-listing fixture
    (ground truth), instead of a hand-built copy that can drift.
    The fixture carries data-match / data-reason per role; the
    structured fields come from the visible text and the reason
    ("deadline in 12 days", "deadline passed 3 days ago",
    "requires final-year students only", "requires 2 years of
    experience")."""
    from html.parser import HTMLParser

    fixture = Path(__file__).parent.parent / "evals" / "sih-benchmark" / "fixtures" / "job-listing.html"
    html = fixture.read_text()

    class _Parser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.roles: list[dict] = []
            self._current: dict | None = None
            self._tag: str | None = None

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "article" and "role" in (attrs.get("class") or ""):
                self._current = {
                    "data_match": attrs.get("data-match"),
                    "data_reason": attrs.get("data-reason"),
                    "title": "", "line": "", "button_id": "",
                }
                self.roles.append(self._current)
            elif self._current is not None and tag in ("h2", "p"):
                self._tag = tag
            elif self._current is not None and tag == "button":
                self._current["button_id"] = attrs.get("id", "")

        def handle_data(self, data):
            if self._current is not None and self._tag in ("h2", "p"):
                self._current["title" if self._tag == "h2" else "line"] += data.strip()

        def handle_endtag(self, tag):
            if tag in ("h2", "p"):
                self._tag = None

    parser = _Parser()
    parser.feed(html)
    roles: list[dict] = []
    for i, raw in enumerate(parser.roles, start=1):
        # "Acme Tech — ₹15,000/month — Remote" (or "FreeWork — Unpaid — Remote").
        parts = [p.strip() for p in raw["line"].split("—")]
        company = parts[0] if parts else ""
        stipend = 0
        location = parts[2] if len(parts) > 2 else ""
        if len(parts) > 1:
            m = re.search(r"₹([\d,]+)", parts[1])
            if m:
                stipend = int(m.group(1).replace(",", ""))
        reason = raw["data_reason"] or ""
        days = 10
        m = re.search(r"deadline in (\d+) days", reason)
        if m:
            days = int(m.group(1))
        m = re.search(r"deadline passed (\d+) days ago", reason)
        if m:
            days = -int(m.group(1))
        roles.append(_role(
            f"role_{i}", raw["title"], company, stipend, location, days,
            min_year=4 if "final-year" in reason else 3,
            requires_experience="experience" in reason,
        ))
        roles[-1]["_expected_match"] = raw["data_match"]
        roles[-1]["_expected_reason"] = reason
    return roles


# The 12 roles, read from the fixture (ground truth).
FIXTURE_ROLES = _parse_fixture_roles()

RESUME_TEXT = """Test User
B.Tech IT, 3rd year, CGPA 8.75
Skills: Python, JavaScript, React, Node.js, SQL, C++
Email: test.user@example.com
Phone: 99999 99999
Looking for SDE intern roles, stipend above 10000, Remote or Hyderabad.
"""


class TestProfile(unittest.TestCase):
    def test_parse_resume(self):
        p = profile.parse_resume(RESUME_TEXT)
        self.assertEqual(p["cgpa"], 8.75)
        self.assertEqual(p["year"], 3)
        self.assertIn("Python", p["skills"])
        self.assertIn("React", p["skills"])
        self.assertEqual(p["email"], "test.user@example.com")
        self.assertIn("Remote", p["locations"])
        self.assertIn("Hyderabad", p["locations"])
        self.assertEqual(p["stipend_min"], 10000)

    def test_parse_resume_missing_fields_are_empty_not_wrong(self):
        p = profile.parse_resume("Just a name, no details.")
        self.assertIsNone(p["cgpa"])
        self.assertEqual(p["skills"], [])
        self.assertIsNone(p["stipend_min"])

    def test_parse_resume_word_boundary_skills(self):
        # Substring matching gave false positives: "C" from
        # "B.Tech", "Go" from "Good", "Java" from "JavaScript",
        # "SQL" from "MySQL".
        p = profile.parse_resume(
            "Test User\nB.Tech IT, 3rd year\nSkills: Python, React\n"
            "Good at JavaScript and MySQL.\n"
        )
        self.assertIn("Python", p["skills"])
        self.assertIn("React", p["skills"])
        self.assertNotIn("C", p["skills"])
        self.assertNotIn("Go", p["skills"])
        self.assertNotIn("Java", p["skills"])
        self.assertNotIn("SQL", p["skills"])
        self.assertIn("JavaScript", p["skills"])
        self.assertIn("MySQL", p["skills"])

    def test_parse_resume_all_caps_name_and_15k_stipend(self):
        p = profile.parse_resume(
            "AKASH GOUD SIDDULURI\nB.Tech IT, 3rd year, CGPA 8.75\n"
            "Looking for SDE intern roles, stipend 15k, Remote.\n"
        )
        self.assertEqual(p["name"], "Akash Goud Sidduluri")
        self.assertEqual(p["stipend_min"], 15000)
        self.assertIn("Remote", p["locations"])

    def test_parse_resume_prefers_the_degree_cgpa(self):
        # A 10th-grade CGPA 9.8 must not beat the B.Tech 8.75.
        p = profile.parse_resume(
            "Test User\n10th grade CGPA 9.8\n"
            "B.Tech IT, 3rd year, CGPA 8.75\n"
        )
        self.assertEqual(p["cgpa"], 8.75)

    def test_parse_resume_address_line_is_not_a_location(self):
        p = profile.parse_resume(
            "Test User\nB.Tech IT, 3rd year\nAddress: 12 Main Road, Chennai 600001\n"
            "Looking for SDE intern roles, Remote.\n"
        )
        self.assertNotIn("Chennai", p["locations"])
        self.assertIn("Remote", p["locations"])


class TestMatcher(unittest.TestCase):
    def test_fixture_roles_five_match_seven_do_not(self):
        # The ground truth in the job-listing fixture: 5 match, 7 do
        # not, each with its reason. The roles are parsed from the
        # fixture itself, and the matcher's verdict is checked against
        # the fixture's data-match per role.
        results = [matcher.match_role(r, profile.TEST_PROFILE, TODAY)
                   for r in FIXTURE_ROLES]
        fits = [m for m in results if m["fits"]]
        excluded = [m for m in results if not m["fits"]]
        self.assertEqual(len(fits), 5, [m["role_id"] for m in fits])
        self.assertEqual(len(excluded), 7, [m["role_id"] for m in excluded])
        for role, result in zip(FIXTURE_ROLES, results):
            expected = role["_expected_match"] == "yes"
            self.assertEqual(
                result["fits"], expected,
                f"{role['id']} ({role['title']} at {role['company']}): "
                f"fixture says {role['_expected_match']}, matcher says {result['fits']} "
                f"({result['missing']})",
            )
        by_id = {m["role_id"]: m for m in results}
        self.assertIn("stipend", by_id["role_6"]["missing"][0].lower())
        self.assertIn("chennai", by_id["role_7"]["missing"][0].lower())
        self.assertIn("deadline passed", by_id["role_8"]["missing"][0].lower())
        self.assertIn("year 4", by_id["role_9"]["missing"][0])
        self.assertIn("not an sde role", by_id["role_10"]["missing"][0].lower())
        self.assertIn("unpaid", by_id["role_11"]["missing"][0].lower())
        self.assertIn("experience", by_id["role_12"]["missing"][0].lower())
        # A fit carries reasons (why it fits) and a score.
        self.assertTrue(by_id["role_1"]["reasons"])
        self.assertGreater(by_id["role_1"]["score"], 50)

    def test_shortlist_is_ranked(self):
        short = matcher.shortlist(FIXTURE_ROLES, profile.TEST_PROFILE, TODAY)
        self.assertEqual(len(short), 5)
        scores = [m["score"] for m in short]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_deadline_passed_is_never_a_fit(self):
        role = _role("role_x", "SDE Intern", "LateCorp", 15000, "Remote", -1)
        m = matcher.match_role(role, profile.TEST_PROFILE, TODAY)
        self.assertFalse(m["fits"])
        self.assertIn("deadline passed", m["missing"][0].lower())

    def test_cgpa_below_the_requirement_is_excluded(self):
        role = _role("role_y", "SDE Intern", "StrictCorp", 15000, "Remote", 10,
                     min_cgpa=9.5)
        m = matcher.match_role(role, profile.TEST_PROFILE, TODAY)
        self.assertFalse(m["fits"])
        self.assertIn("cgpa", m["missing"][0].lower())

    def test_unknown_stipend_is_not_unpaid(self):
        # A role whose stipend is simply not listed is not an
        # unpaid role — it fits, with a warning.
        role = _role("role_z", "SDE Intern", "NoStipendCorp", None, "Remote", 10)
        m = matcher.match_role(role, profile.TEST_PROFILE, TODAY)
        self.assertTrue(m["fits"], m)
        self.assertIn("stipend not listed", m["warnings"])

    def test_live_site_location_with_state_and_country_fits(self):
        role = _role("role_w", "SDE Intern", "LiveCorp", 15000,
                     "Hyderabad, Telangana, India", 10)
        m = matcher.match_role(role, profile.TEST_PROFILE, TODAY)
        self.assertTrue(m["fits"], m)

    def test_degree_requirement_is_checked(self):
        role = _role("role_v", "SDE Intern", "MTechCorp", 15000, "Remote", 10,
                     degree="M.Tech")
        m = matcher.match_role(role, profile.TEST_PROFILE, TODAY)
        self.assertFalse(m["fits"])
        self.assertIn("m.tech", m["missing"][0].lower())

    def test_unparseable_deadline_is_flagged_not_dropped(self):
        role = _role("role_u", "SDE Intern", "HumanDateCorp", 15000, "Remote", 10,
                     deadline="12 Dec 2026")
        m = matcher.match_role(role, profile.TEST_PROFILE, TODAY)
        self.assertTrue(m["fits"], m)
        self.assertTrue(any("deadline not parseable" in w for w in m["warnings"]), m)

    def test_web_designer_is_not_an_sde_role(self):
        for title in ("Web Designer", "Inside Sales Development Representative"):
            role = _role("role_t", title, "NotSDE", 15000, "Remote", 10)
            m = matcher.match_role(role, profile.TEST_PROFILE, TODAY)
            self.assertFalse(m["fits"], title)
            self.assertIn("not an sde role", m["missing"][0].lower())


class TestReceiptsAndApplied(unittest.TestCase):
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

    def test_receipt_recorded_and_listed(self):
        task_id = loop.create_task(self.conn, "Apply to Acme Tech")
        rid = receipts.record_receipt(
            self.conn, task_id, "role_1", "Software Development Intern",
            "Acme Tech", "http://localhost/jobs/role_1",
            "Application submitted. Reference: TEST-001.",
        )
        self.assertTrue(rid)
        listed = receipts.list_receipts(self.conn, task_id)
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["status"], "submitted")
        self.assertIn("TEST-001", listed[0]["confirmation"])
        report = receipts.task_report(self.conn, task_id)
        self.assertEqual(len(report["submitted"]), 1)
        self.assertEqual(len(report["failed"]), 0)

    def test_already_applied_blocks_a_second_submit(self):
        task_id = loop.create_task(self.conn, "Apply to Acme Tech")
        self.assertFalse(applied.is_already_applied(self.conn, "role_1"))
        receipts.record_receipt(
            self.conn, task_id, "role_1", "Software Development Intern",
            "Acme Tech", "http://localhost/jobs/role_1", "Submitted.",
        )
        applied.mark_applied(self.conn, "role_1", "http://localhost/jobs/role_1")
        self.assertTrue(applied.is_already_applied(self.conn, "role_1"))
        self.assertTrue(applied.is_already_applied(
            self.conn, "role_1", "http://localhost/jobs/role_1"))

    def test_failed_receipt_does_not_block_retry(self):
        # A role whose submit failed (a site timeout) must be
        # retryable, not permanently blocked.
        task_id = loop.create_task(self.conn, "Apply to Acme Tech")
        receipts.record_receipt(
            self.conn, task_id, "role_1", "Software Development Intern",
            "Acme Tech", "http://localhost/jobs/role_1",
            "Submit failed: site timeout.", status="failed",
        )
        self.assertFalse(applied.is_already_applied(self.conn, "role_1"))

    def test_duplicate_receipt_returns_the_existing_id(self):
        task_id = loop.create_task(self.conn, "Apply to Acme Tech")
        first = receipts.record_receipt(
            self.conn, task_id, "role_1", "Software Development Intern",
            "Acme Tech", "http://localhost/jobs/role_1", "Submitted.",
        )
        second = receipts.record_receipt(
            self.conn, task_id, "role_1", "Software Development Intern",
            "Acme Tech", "http://localhost/jobs/role_1", "Submitted.",
        )
        self.assertEqual(first, second)
        self.assertEqual(len(receipts.list_receipts(self.conn, task_id)), 1)

    def test_confirmation_is_redacted_before_storage(self):
        task_id = loop.create_task(self.conn, "Apply to Acme Tech")
        receipts.record_receipt(
            self.conn, task_id, "role_1", "Software Development Intern",
            "Acme Tech", "http://localhost/jobs/role_1",
            "Application submitted for test.user@example.com.",
        )
        stored = receipts.list_receipts(self.conn, task_id)[0]
        self.assertNotIn("test.user@example.com", stored["confirmation"])
        self.assertIn("[EMAIL_1]", stored["confirmation"])


class TestApprovalBeforeSubmit(unittest.TestCase):
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

    def _grant(self, task_id):
        import time
        import uuid

        self.conn.execute(
            "INSERT INTO grants (id, origin, scope, expires_at, created_at)"
            " VALUES (?, ?, 'approve', ?, ?)",
            (uuid.uuid4().hex[:12], task_id, int(time.time()) + 600,
             int(time.time())),
        )
        self.conn.commit()

    def test_submit_without_approval_asks_the_user(self):
        # A job-hunt task (goal has "apply") whose step clicks a
        # submit button needs a grant first (§5.4).
        task_id = loop.create_task(self.conn, "Apply to the shortlisted roles")
        loop.plan_task(
            self.conn, task_id,
            call_fn=lambda prompt, **kw: (json.dumps([
                {"step": 1, "description": "click the submit button",
                 "expected_end_state": "application submitted"},
            ]), "mock:planner", 5.0),
        )
        obs = {
            "url": "http://localhost/application-form.html",
            "elements": [
                {"id": "el_app_submit", "tag": "button", "role": "button",
                 "name": "Submit application", "text": "", "value": "",
                 "visible": True, "interactable": True},
            ],
        }
        mock = lambda prompt, **kw: (
            json.dumps({"action": "click", "element_id": "el_app_submit",
                        "reason": "submit the application"}),
            "mock:planner", 5.0,
        )
        with self.assertRaises(ValueError):
            loop.execute_step(self.conn, task_id, 1, obs, call_fn=mock)
        self.assertEqual(loop.get_task(self.conn, task_id)["status"], "ask_user")

    def test_submit_with_a_grant_executes_and_consumes_it(self):
        task_id = loop.create_task(self.conn, "Apply to the shortlisted roles")
        loop.plan_task(
            self.conn, task_id,
            call_fn=lambda prompt, **kw: (json.dumps([
                {"step": 1, "description": "click the submit button",
                 "expected_end_state": "application submitted"},
            ]), "mock:planner", 5.0),
        )
        self._grant(task_id)
        obs = {
            "url": "http://localhost/application-form.html",
            "elements": [
                {"id": "el_app_submit", "tag": "button", "role": "button",
                 "name": "Submit application", "text": "", "value": "",
                 "visible": True, "interactable": True},
            ],
        }
        mock = lambda prompt, **kw: (
            json.dumps({"action": "click", "element_id": "el_app_submit",
                        "reason": "submit the application"}),
            "mock:planner", 5.0,
        )
        result = loop.execute_step(self.conn, task_id, 1, obs, call_fn=mock)
        self.assertEqual(result["action"]["element_id"], "el_app_submit")
        # The grant is consumed: a second submit needs a fresh one.
        grants = self.conn.execute(
            "SELECT COUNT(*) AS c FROM grants WHERE origin = ?", (task_id,)
        ).fetchone()
        self.assertEqual(grants["c"], 0)

    def test_login_submit_does_not_need_approval(self):
        # The login-fixture flow (goal "Log in…") is not an
        # application, so no grant is required (existing behaviour).
        task_id = loop.create_task(self.conn, "Log in to the test portal")
        loop.plan_task(
            self.conn, task_id,
            call_fn=lambda prompt, **kw: (json.dumps([
                {"step": 1, "description": "click the log in button",
                 "expected_end_state": "application form opens"},
            ]), "mock:planner", 5.0),
        )
        obs = {
            "url": "http://localhost/login.html",
            "elements": [
                {"id": "el_login_submit", "tag": "button", "role": "button",
                 "name": "Log in", "text": "", "value": "",
                 "visible": True, "interactable": True},
            ],
        }
        mock = lambda prompt, **kw: (
            json.dumps({"action": "click", "element_id": "el_login_submit"}),
            "mock:planner", 5.0,
        )
        result = loop.execute_step(self.conn, task_id, 1, obs, call_fn=mock)
        self.assertEqual(result["action"]["element_id"], "el_login_submit")

    def _two_step_apply_task(self):
        """A job-listing task whose step 1 clicks el_apply_1 and is
        done (what verify_step leaves behind after a good click)."""
        task_id = loop.create_task(self.conn, "Apply to the shortlisted roles")
        loop.plan_task(
            self.conn, task_id,
            call_fn=lambda prompt, **kw: (json.dumps([
                {"step": 1, "description": "click the apply button",
                 "expected_end_state": "application form opens"},
                {"step": 2, "description": "click the next button",
                 "expected_end_state": "next section opens"},
            ]), "mock:planner", 5.0),
        )
        obs = {
            "url": "http://localhost/job-listing.html",
            "elements": [
                {"id": "el_apply_1", "tag": "button", "role": "button",
                 "name": "Apply", "text": "", "value": "",
                 "visible": True, "interactable": True},
                {"id": "el_next", "tag": "button", "role": "button",
                 "name": "Next", "text": "", "value": "",
                 "visible": True, "interactable": True},
            ],
        }
        mock = lambda prompt, **kw: (
            json.dumps({"action": "click", "element_id": "el_apply_1"}),
            "mock:planner", 5.0,
        )
        loop.execute_step(self.conn, task_id, 1, obs, call_fn=mock)
        self.conn.execute(
            "UPDATE steps SET status = 'done' WHERE task_id = ? AND n = 1",
            (task_id,),
        )
        self.conn.commit()
        return task_id, obs

    def test_repeated_click_replans_with_feedback(self):
        # Fix list item 2 prep: the live planner repeated
        # "click el_apply_1" and stalled the job-listing fixture.
        # A repeat is now a re-plan with feedback, not an instant
        # step failure — the second attempt picks another element.
        task_id, obs = self._two_step_apply_task()
        prompts_seen: list[str] = []
        calls = {"n": 0}

        def mock(prompt, **kw):
            prompts_seen.append(prompt)
            calls["n"] += 1
            element = "el_apply_1" if calls["n"] == 1 else "el_next"
            return (
                json.dumps({"action": "click", "element_id": element}),
                "mock:planner", 5.0,
            )

        result = loop.execute_step(self.conn, task_id, 2, obs, call_fn=mock)
        self.assertEqual(result["action"]["element_id"], "el_next")
        self.assertEqual(calls["n"], 2)
        self.assertIn("already clicked", prompts_seen[1])
        self.assertIn("el_apply_1", prompts_seen[1])

    def test_repeated_click_three_times_fails_the_step(self):
        # A planner that keeps repeating the same click exhausts
        # the 3-call budget: the step fails and the task asks the
        # user — still fail-closed, never a silent re-submit.
        from server.planner import validate

        task_id, obs = self._two_step_apply_task()
        mock = lambda prompt, **kw: (
            json.dumps({"action": "click", "element_id": "el_apply_1"}),
            "mock:planner", 5.0,
        )
        with self.assertRaises(validate.ValidationError) as ctx:
            loop.execute_step(self.conn, task_id, 2, obs, call_fn=mock)
        self.assertIn("could not produce a valid action in 3 calls", str(ctx.exception))
        self.assertIn("already clicked", str(ctx.exception))
        step = loop.get_task(self.conn, task_id)
        self.assertEqual(step["status"], "ask_user")

    def test_approval_required_for_buy_send_and_mail_goals(self):
        # §5.4: purchases and email sends need a fresh approval —
        # not just applications.
        for goal in (
            "Buy this book with my saved card",
            "Cold-mail recruiters for the shortlisted roles",
            "Send the report to my manager",
        ):
            task = {"goal": goal, "constraints": ""}
            self.assertTrue(loop._requires_approval(task), goal)
        self.assertFalse(loop._requires_approval(
            {"goal": "Log in to the test portal", "constraints": ""}))

    def test_consequential_is_type_and_form_not_just_name(self):
        # A "Continue" button inside a form is a submit; an icon-only
        # submit button is a submit; an "Apply now" button NOT in a
        # form only opens the form — it is not a submit.
        continue_btn = {
            "id": "el_continue", "tag": "button", "role": "button",
            "name": "Continue", "text": "", "type": "button",
            "in_form": True, "visible": True, "interactable": True,
        }
        icon_submit = {
            "id": "el_icon", "tag": "button", "role": "button",
            "name": "", "text": "", "type": "submit",
            "in_form": True, "visible": True, "interactable": True,
        }
        apply_now = {
            "id": "el_apply_1", "tag": "button", "role": "button",
            "name": "Apply now", "text": "", "type": "button",
            "in_form": False, "visible": True, "interactable": True,
        }
        elements = [continue_btn, icon_submit, apply_now]
        self.assertTrue(loop._is_consequential(
            {"action": "click", "element_id": "el_continue"}, elements))
        self.assertTrue(loop._is_consequential(
            {"action": "click", "element_id": "el_icon"}, elements))
        self.assertFalse(loop._is_consequential(
            {"action": "click", "element_id": "el_apply_1"}, elements))

    def test_already_applied_is_enforced_in_the_submit_path(self):
        # §6.9: the already-applied check is a gate in execute_step,
        # not just a queryable endpoint. A role applied to in an
        # earlier task is never re-submitted.
        first = loop.create_task(
            self.conn, "Apply to Acme Tech",
            constraints="role: role_1\nurl: http://localhost/jobs/role_1",
        )
        receipts.record_receipt(
            self.conn, first, "role_1", "Software Development Intern",
            "Acme Tech", "http://localhost/jobs/role_1", "Submitted.",
        )
        applied.mark_applied(self.conn, "role_1", "http://localhost/jobs/role_1")
        second = loop.create_task(
            self.conn, "Apply to Acme Tech again",
            constraints="role: role_1\nurl: http://localhost/jobs/role_1",
        )
        loop.plan_task(
            self.conn, second,
            call_fn=lambda prompt, **kw: (json.dumps([
                {"step": 1, "description": "click the submit button",
                 "expected_end_state": "application submitted"},
            ]), "mock:planner", 5.0),
        )
        obs = {
            "url": "http://localhost/application-form.html",
            "elements": [
                {"id": "el_app_submit", "tag": "button", "role": "button",
                 "name": "Submit application", "text": "", "value": "",
                 "type": "submit", "in_form": True,
                 "visible": True, "interactable": True},
            ],
        }
        mock = lambda prompt, **kw: (
            json.dumps({"action": "click", "element_id": "el_app_submit",
                        "reason": "submit the application"}),
            "mock:planner", 5.0,
        )
        with self.assertRaises(ValueError):
            loop.execute_step(self.conn, second, 1, obs, call_fn=mock)
        self.assertEqual(loop.get_task(self.conn, second)["status"], "ask_user")

    def test_grant_scoped_to_one_element_cannot_be_spent_on_another(self):
        # A grant whose detail names element A only covers A.
        task_id = loop.create_task(self.conn, "Apply to the shortlisted roles")
        loop.plan_task(
            self.conn, task_id,
            call_fn=lambda prompt, **kw: (json.dumps([
                {"step": 1, "description": "click the submit button",
                 "expected_end_state": "application submitted"},
            ]), "mock:planner", 5.0),
        )
        import time
        import uuid

        self.conn.execute(
            "INSERT INTO grants (id, origin, scope, expires_at, created_at, detail)"
            " VALUES (?, ?, 'approve', ?, ?, ?)",
            (uuid.uuid4().hex[:12], task_id, int(time.time()) + 600,
             int(time.time()), json.dumps({"element_id": "el_app_submit"})),
        )
        self.conn.commit()
        obs = {
            "url": "http://localhost/application-form.html",
            "elements": [
                {"id": "el_other_submit", "tag": "button", "role": "button",
                 "name": "Submit application", "text": "", "value": "",
                 "type": "submit", "in_form": True,
                 "visible": True, "interactable": True},
            ],
        }
        mock = lambda prompt, **kw: (
            json.dumps({"action": "click", "element_id": "el_other_submit",
                        "reason": "submit the application"}),
            "mock:planner", 5.0,
        )
        with self.assertRaises(ValueError):
            loop.execute_step(self.conn, task_id, 1, obs, call_fn=mock)

    def test_grant_double_spend_under_concurrency(self):
        # Two threads racing to consume one grant: exactly one wins.
        import threading
        import time
        import uuid

        task_id = loop.create_task(self.conn, "Apply to the shortlisted roles")
        self.conn.execute(
            "INSERT INTO grants (id, origin, scope, expires_at, created_at)"
            " VALUES (?, ?, 'approve', ?, ?)",
            (uuid.uuid4().hex[:12], task_id, int(time.time()) + 600,
             int(time.time())),
        )
        self.conn.commit()
        results = []

        def consume():
            # Each thread needs its own connection (sqlite3
            # connections are not shared across threads).
            conn = db.connect()
            try:
                results.append(loop._consume_approval(conn, task_id))
            finally:
                conn.close()

        threads = [threading.Thread(target=consume) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(results), [False, True])


class TestJobHuntEndToEnd(unittest.TestCase):
    """The Sprint 4 flow on the fixtures: profile → match → shortlist
    → apply to one role with approval → verify → receipt."""

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

    def test_job_hunt_flow(self):
        # 1. The resume is shared; the profile is stored locally.
        profile.store_profile(self.conn, profile.parse_resume(RESUME_TEXT))
        stored = profile.get_profile(self.conn)
        self.assertEqual(stored["cgpa"], 8.75)
        # 2. Match the 12 fixture roles; the shortlist is 5.
        short = matcher.shortlist(FIXTURE_ROLES, stored, TODAY)
        self.assertEqual(len(short), 5)
        chosen = short[0]
        self.assertIn("role_", chosen["role_id"])
        # 3. Apply to the chosen role: a task whose steps fill the
        #    form and submit it (mock planner, application-form obs).
        #    The role rides in the constraints, so the loop can
        #    check "already applied" and record the receipt.
        task_id = loop.create_task(
            self.conn, f"Apply to {chosen['role_id']} (Acme Tech)",
            constraints=(
                f"role: {chosen['role_id']}\n"
                f"url: http://localhost/jobs/{chosen['role_id']}"
            ),
        )
        loop.plan_task(
            self.conn, task_id,
            call_fn=lambda prompt, **kw: (json.dumps([
                {"step": 1, "description": "type the name into the name field",
                 "expected_end_state": "name field holds a value"},
                {"step": 2, "description": "click the submit button",
                 "expected_end_state": "application submitted"},
            ]), "mock:planner", 5.0),
        )
        form_obs = {
            "url": "http://localhost/application-form.html",
            "elements": [
                {"id": "el_app_name", "tag": "input", "role": "textbox",
                 "name": "Full name", "text": "", "value": "",
                 "visible": True, "interactable": True},
                {"id": "el_app_submit", "tag": "button", "role": "button",
                 "name": "Submit application", "text": "", "value": "",
                 "visible": True, "interactable": True},
            ],
        }
        # Step 1: type the name (from the profile, as a placeholder).
        r1 = loop.execute_step(
            self.conn, task_id, 1, form_obs,
            call_fn=lambda prompt, **kw: (
                json.dumps({"action": "type", "element_id": "el_app_name",
                            "text": "[NAME_1]"}), "mock:planner", 5.0),
        )
        self.assertEqual(r1["action"]["element_id"], "el_app_name")
        filled = {
            "url": "http://localhost/application-form.html",
            "elements": [
                {"id": "el_app_name", "tag": "input", "role": "textbox",
                 "name": "Full name", "text": "", "value": "[NAME_1]",
                 "visible": True, "interactable": True},
                {"id": "el_app_submit", "tag": "button", "role": "button",
                 "name": "Submit application", "text": "", "value": "",
                 "visible": True, "interactable": True},
            ],
        }
        v1 = loop.verify_step(
            self.conn, task_id, 1, filled, r1["expected_end_state"],
            previous_url=form_obs["url"], previous_observation=form_obs,
        )
        self.assertTrue(v1["verified"], v1)
        # Step 2: the submit needs approval first.
        mock_submit = lambda prompt, **kw: (
            json.dumps({"action": "click", "element_id": "el_app_submit",
                        "reason": "submit the application"}),
            "mock:planner", 5.0,
        )
        with self.assertRaises(ValueError):
            loop.execute_step(self.conn, task_id, 2, form_obs, call_fn=mock_submit)
        self.assertEqual(loop.get_task(self.conn, task_id)["status"], "ask_user")
        # The user approves (a grant), then the submit executes.
        import time
        import uuid

        self.conn.execute(
            "INSERT INTO grants (id, origin, scope, expires_at, created_at)"
            " VALUES (?, ?, 'approve', ?, ?)",
            (uuid.uuid4().hex[:12], task_id, int(time.time()) + 600,
             int(time.time())),
        )
        self.conn.commit()
        r2 = loop.execute_step(self.conn, task_id, 2, form_obs, call_fn=mock_submit)
        self.assertEqual(r2["action"]["element_id"], "el_app_submit")
        # Verify by the URL changing to the confirmation page.
        done_obs = {"url": "http://localhost/confirmation.html", "elements": []}
        v2 = loop.verify_step(
            self.conn, task_id, 2, done_obs, r2["expected_end_state"],
            previous_url=form_obs["url"], previous_observation=form_obs,
        )
        self.assertTrue(v2["verified"], v2)
        self.assertEqual(loop.get_task(self.conn, task_id)["status"], "done")
        # 4. The verified submit recorded its own receipt (§6.9) —
        #    no external POST needed. The task's report shows it.
        report = receipts.task_report(self.conn, task_id)
        self.assertEqual(len(report["submitted"]), 1)
        # 5. The same role cannot be applied to again.
        self.assertTrue(applied.is_already_applied(self.conn, chosen["role_id"]))


if __name__ == "__main__":
    unittest.main()


# --- API tests (live server, same pattern as test_tasks.TestTasksApi) ---

import socket as _socket
import subprocess as _subprocess
import sys as _sys
import urllib.error as _urlerror
import urllib.request as _urlrequest

_JOBS_TOKEN = "test-token-jobs"
_REPO_ROOT = Path(__file__).parent.parent


def _free_port() -> int:
    with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _request(base_url, method, path, token=_JOBS_TOKEN, body=None):
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


class TestJobsApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.port = _free_port()
        cls.base = f"http://127.0.0.1:{cls.port}"
        env = dict(os.environ)
        env["ECLIPSE_DATA_DIR"] = cls.tmp.name
        env["ECLIPSE_TOKEN"] = _JOBS_TOKEN
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
            raise RuntimeError("server did not come up for jobs API test")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(timeout=10)
        cls.tmp.cleanup()

    def test_profile_round_trip_and_match(self):
        status, body = _request(
            self.base, "POST", "/jobs/profile",
            body={"resume_text": RESUME_TEXT},
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["profile"]["cgpa"], 8.75)
        status, body = _request(self.base, "GET", "/jobs/profile")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["profile"]["year"], 3)
        roles = [
            {"id": r["id"], "title": r["title"], "company": r["company"],
             "url": r["url"], "stipend": r["stipend"],
             "location": r["location"], "deadline": r["deadline"],
             "skills": r["skills"], "min_year": r["min_year"],
             "min_cgpa": r["min_cgpa"],
             "requires_experience": r["requires_experience"]}
            for r in FIXTURE_ROLES
        ]
        status, body = _request(self.base, "POST", "/jobs/match", body={"roles": roles})
        self.assertEqual(status, 200, body)
        self.assertEqual(len(body["shortlist"]), 5)
        self.assertEqual(len(body["excluded"]), 7)

    def test_stipend_as_a_string_is_parsed(self):
        # A site returning "₹15,000" as a string must not 422.
        status, body = _request(
            self.base, "POST", "/jobs/profile",
            body={"resume_text": RESUME_TEXT},
        )
        self.assertEqual(status, 200, body)
        status, body = _request(
            self.base, "POST", "/jobs/match",
            body={"roles": [{
                "id": "role_x", "title": "SDE Intern", "company": "StrCorp",
                "url": "http://localhost/jobs/role_x", "stipend": "₹15,000",
                "location": "Remote",
                "deadline": str(TODAY + _dt.timedelta(days=10)),
                "skills": ["Python"], "min_year": 3,
            }]},
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(len(body["shortlist"]), 1, body)

    def test_get_task_returns_receipts_and_revoke_works(self):
        # §6.1: GET /tasks/{id} returns receipts. §5.4: a grant
        # can be revoked in one click.
        status, body = _request(
            self.base, "POST", "/tasks",
            body={"goal": "Apply to Acme Tech"},
        )
        self.assertEqual(status, 201, body)
        task_id = body["id"]
        status, body = _request(
            self.base, "POST", f"/tasks/{task_id}/receipts",
            body={"role_id": "role_1", "title": "Software Development Intern",
                  "company": "Acme Tech", "url": "http://localhost/jobs/role_1",
                  "confirmation": "Application submitted."},
        )
        self.assertEqual(status, 200, body)
        status, body = _request(self.base, "GET", f"/tasks/{task_id}")
        self.assertEqual(status, 200, body)
        self.assertEqual(len(body["receipts"]), 1)
        self.assertEqual(body["receipts"][0]["role_id"], "role_1")
        # A duplicate receipt for the same role is refused.
        status, body = _request(
            self.base, "POST", f"/tasks/{task_id}/receipts",
            body={"role_id": "role_1", "title": "Software Development Intern",
                  "company": "Acme Tech", "url": "http://localhost/jobs/role_1",
                  "confirmation": "Application submitted."},
        )
        self.assertEqual(status, 409, body)
        # Approve (creates a grant), then revoke it. Bug 3 fix:
        # approve needs a LIVE pending approval — seed one in the
        # server's DB first (the old test approved a bare id,
        # which is exactly the hole the fix closes).
        conn = db.connect(Path(self.tmp.name) / "eclipse.db")
        try:
            approval_id = loop.record_pending_approval(
                conn, task_id, step=1, action="submit")
        finally:
            conn.close()
        status, body = _request(
            self.base, "POST", f"/tasks/{task_id}/approve",
            body={"approval_id": approval_id},
        )
        self.assertEqual(status, 200, body)
        status, body = _request(
            self.base, "DELETE", f"/tasks/{task_id}/grants",
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["revoked"], 1)

    def test_jobs_require_token(self):
        status, _ = _request(
            self.base, "GET", "/jobs/profile", token=None,
        )
        self.assertEqual(status, 401)
