"""Mission discovery tests (PR #5, W2): Gmail offers, pasted
roles, watch events — with the honest limits (no fake search,
no canned roles).

Run: .venv/bin/python -m unittest tests.test_missions_discovery -v
All data fake; the Gmail connector is an injected double.
"""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from server.connectors.base import ConnectorError
from server.connectors import registry as conn_registry
from server.core import config, db
from server.jobs import profile as profile_mod
from server.missions import discovery, models
from server.monitor import watches as watch_mod

ROLE_FIT = {
    "id": "role_fit",
    "title": "Software Engineer Intern",
    "company": "Example Labs",
    "site": "example",
    "url": "https://example.com/jobs/role_fit",
    "stipend": 15000,
    "location": "Remote",
    "deadline": None,
    "skills": ["Python"],
    "min_year": None,
    "degree": None,
    "min_cgpa": None,
    "requires_experience": False,
}

ROLE_NOFIT = dict(ROLE_FIT, id="role_nofit", title="Marketing Intern",
                  stipend=5000)


class FakeGmail:
    """A Gmail double returning canned offer mails."""

    def __init__(self, messages):
        self.messages = messages
        self.calls = 0

    def search_offers(self, max_results=20):
        self.calls += 1
        return self.messages[:max_results]


class RaisingGmail:
    def search_offers(self, max_results=20):
        raise ConnectorError("gmail", "inbox unreachable", "unavailable")


class DiscoveryTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old = config.DATA_DIR
        config.DATA_DIR = Path(self.tmp.name)
        db.migrate()
        self.conn = db.connect()
        profile_mod.store_profile(self.conn, dict(profile_mod.TEST_PROFILE))

    def tearDown(self):
        self.conn.close()
        config.DATA_DIR = self._old
        self.tmp.cleanup()

    def _mission(self, mission_type="job_hunt", config=None, bounds=None,
                 activate=True):
        mission = models.create_mission(
            self.conn, mission_type, f"Test {mission_type}",
            config or {}, bounds or {},
        )
        if activate:
            mission = models.transition(self.conn, mission["id"], "active")
        return mission


class TestGmailOffers(DiscoveryTestCase):
    def test_offer_mail_becomes_a_matched_candidate(self):
        gmail = FakeGmail([{
            "id": "mail_1",
            "from": "hiring@example.com",
            "subject": "Software Engineer Intern — Example Labs",
            "date": "2026-11-23",
            "body_redacted": "We would like to interview you for the"
            " Software Engineer Intern role. Stipend ₹15,000, Remote.",
        }])
        mission = self._mission(config={"sources": ["gmail"]})
        found = discovery.discover_gmail_offers(self.conn, mission, gmail)
        self.assertEqual(len(found), 1)
        candidate = found[0]
        self.assertEqual(candidate["source"], "gmail")
        self.assertEqual(candidate["role"]["title"], "Software Engineer Intern")
        self.assertTrue(candidate["match"]["fits"])
        self.assertGreaterEqual(candidate["match"]["score"], 60)
        self.assertFalse(candidate["needs_confirmation"])

    def test_newsletter_mail_is_low_confidence_never_fits(self):
        gmail = FakeGmail([{
            "id": "mail_2",
            "from": "news@example.com",
            "subject": "Your weekly digest: selected stories",
            "date": "2026-11-23",
            "body_redacted": "Here are this week's selected stories.",
        }])
        mission = self._mission(config={"sources": ["gmail"]})
        found = discovery.discover_gmail_offers(self.conn, mission, gmail)
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0]["needs_confirmation"])
        self.assertFalse(found[0]["match"]["fits"])

    def test_gmail_off_skips_discovery_without_failing(self):
        conn_registry.set_enabled(self.conn, "gmail", False)
        gmail = FakeGmail([{"id": "mail_1", "from": "a@example.com",
                            "subject": "Software Engineer Intern",
                            "date": "", "body_redacted": ""}])
        mission = self._mission(config={"sources": ["gmail"]})
        found = discovery.discover_gmail_offers(self.conn, mission, gmail)
        self.assertEqual(found, [])
        self.assertEqual(gmail.calls, 0)
        self.assertFalse(discovery.gmail_available(self.conn))

    def test_gmail_connector_error_propagates(self):
        mission = self._mission(config={"sources": ["gmail"]})
        with self.assertRaises(ConnectorError):
            discovery.discover_gmail_offers(
                self.conn, mission, RaisingGmail())

    def test_gmail_dedup_against_discovered_activity(self):
        gmail = FakeGmail([{
            "id": "mail_1", "from": "hiring@example.com",
            "subject": "Software Engineer Intern",
            "date": "", "body_redacted": "",
        }])
        mission = self._mission(config={"sources": ["gmail"]})
        first = discovery.discover_gmail_offers(self.conn, mission, gmail)
        self.assertEqual(len(first), 1)
        models.record_activity(
            self.conn, mission["id"], "discovered",
            {"role_id": "gmail:mail_1"})
        second = discovery.discover_gmail_offers(self.conn, mission, gmail)
        self.assertEqual(second, [])


class TestPastedRoles(DiscoveryTestCase):
    def test_pasted_roles_matched_and_deduped(self):
        mission = self._mission(
            config={"sources": ["pasted"], "roles": [ROLE_FIT, ROLE_NOFIT]})
        found = discovery.discover_pasted_roles(self.conn, mission)
        self.assertEqual(len(found), 2)
        by_id = {c["role"]["id"]: c for c in found}
        self.assertTrue(by_id["role_fit"]["match"]["fits"])
        self.assertFalse(by_id["role_nofit"]["match"]["fits"])
        # Record both as discovered; a second discovery is empty.
        for candidate in found:
            models.record_activity(
                self.conn, mission["id"], "discovered",
                {"role_id": candidate["role"]["id"]})
        self.assertEqual(
            discovery.discover_pasted_roles(self.conn, mission), [])

    def test_no_profile_means_no_match(self):
        self.conn.execute("DELETE FROM memory WHERE key = 'profile'")
        self.conn.commit()
        mission = self._mission(
            config={"sources": ["pasted"], "roles": [ROLE_FIT]})
        found = discovery.discover_pasted_roles(self.conn, mission)
        self.assertEqual(len(found), 1)
        self.assertIsNone(found[0]["match"])

    def test_mission_stipend_override_beats_profile_floor(self):
        # The profile floor is 10000; the mission floor is 20000,
        # so the 15000 role no longer fits.
        mission = self._mission(config={
            "sources": ["pasted"], "roles": [ROLE_FIT],
            "stipend_min": 20000,
        })
        found = discovery.discover_pasted_roles(self.conn, mission)
        self.assertFalse(found[0]["match"]["fits"])


class TestColdMailDiscovery(DiscoveryTestCase):
    def test_role_ids_resolve_from_job_hunt_missions(self):
        self._mission(config={"sources": ["pasted"], "roles": [ROLE_FIT]})
        mail = self._mission(
            "cold_mail",
            config={"role_ids": ["role_fit"],
                    "recipients": ["recruiter@example.com"]})
        found = discovery.discover_cold_mail(self.conn, mail)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["role"]["id"], "role_fit")
        self.assertTrue(found[0]["match"]["fits"])

    def test_unknown_role_id_resolves_to_nothing(self):
        mail = self._mission(
            "cold_mail",
            config={"role_ids": ["role_missing"],
                    "recipients": ["recruiter@example.com"]})
        self.assertEqual(discovery.discover_cold_mail(self.conn, mail), [])


class TestWatchDiscovery(DiscoveryTestCase):
    def _price_mission(self, **config):
        watch = watch_mod.create_watch(
            self.conn, "https://example.com/product/1", ".price",
            "changed", interval_min=15,
        )
        base = {"url": "https://example.com/product/1",
                "selector": ".price", "watch_id": watch["id"]}
        base.update(config)
        mission = self._mission("price_watch", config=base)
        return mission, watch

    def test_fired_event_becomes_a_price_candidate(self):
        mission, watch = self._price_mission(target_price=5000)
        watch_mod.check_watch(self.conn, watch["id"], True, "₹6,999")
        fired = watch_mod.check_watch(self.conn, watch["id"], True, "₹4,999")
        self.assertTrue(fired["fired"])
        found = discovery.discover_price_watch(self.conn, mission)
        self.assertEqual(len(found), 1)
        candidate = found[0]
        self.assertEqual(candidate["kind"], "price_event")
        self.assertEqual(candidate["source"], "watch")
        self.assertEqual(candidate["detail"]["observed_price"], 4999.0)

    def test_no_watch_means_no_discovery(self):
        mission = self._mission("price_watch", config={
            "url": "https://example.com/product/1", "selector": ".price"})
        self.assertEqual(discovery.discover_price_watch(self.conn, mission), [])

    def test_fired_event_deduped_after_recording(self):
        mission, watch = self._price_mission()
        watch_mod.check_watch(self.conn, watch["id"], True, "₹6,999")
        watch_mod.check_watch(self.conn, watch["id"], True, "₹4,999")
        first = discovery.discover_price_watch(self.conn, mission)
        self.assertEqual(len(first), 1)
        models.record_activity(
            self.conn, mission["id"], "discovered",
            {"event_id": first[0]["detail"]["event_id"]})
        self.assertEqual(
            discovery.discover_price_watch(self.conn, mission), [])


class TestDiscoverDispatch(DiscoveryTestCase):
    def test_discover_dispatches_by_type(self):
        mission = self._mission(
            config={"sources": ["pasted"], "roles": [ROLE_FIT]})
        found = discovery.discover(self.conn, mission)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["kind"], "role")

    def test_discover_unknown_type_finds_nothing(self):
        found = discovery.discover(self.conn, {"id": "m_x", "type": "nope"})
        self.assertEqual(found, [])

    def test_parse_price(self):
        self.assertEqual(discovery.parse_price("₹4,999"), 4999.0)
        self.assertEqual(discovery.parse_price("Rs. 4999.00"), 4999.0)
        self.assertEqual(discovery.parse_price("$49.99"), 49.99)
        self.assertIsNone(discovery.parse_price("out of stock"))
        self.assertIsNone(discovery.parse_price(None))

    def test_redact_candidate_text(self):
        clean = discovery.redact_candidate_text(
            "saw role at recruiter@example.com")
        self.assertNotIn("recruiter@example.com", clean)
        self.assertIn("[EMAIL_1]", clean)


if __name__ == "__main__":
    unittest.main()
