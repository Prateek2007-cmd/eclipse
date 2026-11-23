"""W1 bounds tests — IST-day cap counting, check_bounds purity,
and the Q1 standing-approval auto-satisfy semantics (PR #5, §3.5).

Mock planners only — no API key, no live model, no network. All
test data is fake. See tests/test_missions_core.py for the note on
the types.py stand-in (W2's file): these tests use the REAL
registry when importable, else the stand-in below, which
implements exactly the §3.5/§3.7 contract.
"""
import json
import os
import sys
import tempfile
import time
import unittest
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import re
import types as _stdlib_types
from dataclasses import dataclass, field

# --- types.py stand-in (W2's file; see the module docstring) ---
try:
    from server.missions import types as _real_types  # noqa: F401

    USING_STANDIN_TYPES = False
except ImportError:
    USING_STANDIN_TYPES = True

    _HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

    @dataclass
    class MissionTypeSpec:
        type: str
        label: str
        config_schema: dict
        default_bounds: dict
        default_interval_min: int = 60
        discover: object = None
        act: object = None

    def _is_int(v):
        return type(v) is int

    def _is_bool(v):
        return type(v) is bool

    def _validate_config_job_hunt(config):
        allowed = {"sources", "min_score", "roles", "watch_new_only"}
        unknown = set(config) - allowed
        if unknown:
            raise ValueError(f"unknown config keys: {sorted(unknown)}")
        out = {"sources": ["gmail"], "min_score": 60, "roles": [],
               "watch_new_only": True}
        if "sources" in config:
            if not isinstance(config["sources"], list):
                raise ValueError("sources must be a list")
            out["sources"] = list(config["sources"])
        if "min_score" in config:
            if not _is_int(config["min_score"]) or not 0 <= config["min_score"] <= 100:
                raise ValueError("min_score must be an int 0-100")
            out["min_score"] = config["min_score"]
        if "roles" in config:
            if not isinstance(config["roles"], list):
                raise ValueError("roles must be a list")
            out["roles"] = list(config["roles"])
        if "watch_new_only" in config:
            if not _is_bool(config["watch_new_only"]):
                raise ValueError("watch_new_only must be a bool")
            out["watch_new_only"] = config["watch_new_only"]
        return out

    def _validate_config_cold_mail(config):
        allowed = {"role_ids", "recipients", "attach_resume",
                   "max_drafts_per_run"}
        unknown = set(config) - allowed
        if unknown:
            raise ValueError(f"unknown config keys: {sorted(unknown)}")
        out = {"role_ids": [], "recipients": [], "attach_resume": False,
               "max_drafts_per_run": 5}
        for key in ("role_ids", "recipients"):
            if key in config:
                if not isinstance(config[key], list):
                    raise ValueError(f"{key} must be a list")
                out[key] = list(config[key])
        if "attach_resume" in config:
            if not _is_bool(config["attach_resume"]):
                raise ValueError("attach_resume must be a bool")
            out["attach_resume"] = config["attach_resume"]
        if "max_drafts_per_run" in config:
            if not _is_int(config["max_drafts_per_run"]) or config["max_drafts_per_run"] < 1:
                raise ValueError("max_drafts_per_run must be an int >= 1")
            out["max_drafts_per_run"] = config["max_drafts_per_run"]
        return out

    def _validate_config_price_watch(config):
        allowed = {"url", "selector", "condition", "target_price",
                   "currency", "one_shot"}
        unknown = set(config) - allowed
        if unknown:
            raise ValueError(f"unknown config keys: {sorted(unknown)}")
        for key in ("url", "selector"):
            if not config.get(key) or not isinstance(config[key], str):
                raise ValueError(f"{key} is required for price_watch")
        out = {"url": config["url"], "selector": config["selector"],
               "condition": "changed", "target_price": None,
               "currency": "INR", "one_shot": True}
        if "condition" in config:
            cond = config["condition"]
            if cond not in ("changed", "appeared", "disappeared") and not (
                    isinstance(cond, str) and cond.startswith("contains:")):
                raise ValueError(f"unknown condition {cond!r}")
            out["condition"] = cond
        if "target_price" in config:
            if config["target_price"] is not None and (
                    not _is_int(config["target_price"])
                    or config["target_price"] < 0):
                raise ValueError("target_price must be an int >= 0 or null")
            out["target_price"] = config["target_price"]
        if "currency" in config:
            if not isinstance(config["currency"], str):
                raise ValueError("currency must be a string")
            out["currency"] = config["currency"]
        if "one_shot" in config:
            if not _is_bool(config["one_shot"]):
                raise ValueError("one_shot must be a bool")
            out["one_shot"] = config["one_shot"]
        return out

    _CONFIG_VALIDATORS = {
        "job_hunt": _validate_config_job_hunt,
        "cold_mail": _validate_config_cold_mail,
        "price_watch": _validate_config_price_watch,
    }

    _BOUNDS_KEYS = {"max_actions_per_day", "max_applies_per_day",
                    "max_sends_per_day", "max_spend_inr", "allowed_sites",
                    "require_approval_for", "auto_approve",
                    "quiet_hours_ist", "min_score"}

    def _default_bounds_for(mission_type):
        return {
            "max_actions_per_day": 5,
            "max_applies_per_day": 3 if mission_type == "job_hunt" else 0,
            "max_sends_per_day": 5 if mission_type == "cold_mail" else 0,
            "max_spend_inr": 0,
            "allowed_sites": [],
            "require_approval_for": ["apply", "send"],
            "auto_approve": False,
            "quiet_hours_ist": None,
            "min_score": 60,
        }

    def validate_config(mission_type, config):
        if mission_type not in _CONFIG_VALIDATORS:
            raise ValueError(f"unknown mission type {mission_type!r}")
        if not isinstance(config, dict):
            raise ValueError("config must be an object")
        return _CONFIG_VALIDATORS[mission_type](config)

    def validate_bounds(mission_type, bounds):
        if mission_type not in _CONFIG_VALIDATORS:
            raise ValueError(f"unknown mission type {mission_type!r}")
        if not isinstance(bounds, dict):
            raise ValueError("bounds must be an object")
        unknown = set(bounds) - _BOUNDS_KEYS
        if unknown:
            raise ValueError(f"unknown bounds keys: {sorted(unknown)}")
        out = _default_bounds_for(mission_type)
        for key in ("max_actions_per_day", "max_applies_per_day",
                    "max_sends_per_day", "max_spend_inr"):
            if key in bounds:
                if not _is_int(bounds[key]) or bounds[key] < 0:
                    raise ValueError(f"{key} must be an int >= 0")
                out[key] = bounds[key]
        if "allowed_sites" in bounds:
            if not isinstance(bounds["allowed_sites"], list):
                raise ValueError("allowed_sites must be a list")
            out["allowed_sites"] = list(bounds["allowed_sites"])
        if "require_approval_for" in bounds:
            req = bounds["require_approval_for"]
            if not isinstance(req, list) or not set(req) <= {"apply", "send"}:
                raise ValueError(
                    "require_approval_for must be a subset of"
                    " ['apply', 'send']")
            out["require_approval_for"] = list(req)
        if "auto_approve" in bounds:
            if not _is_bool(bounds["auto_approve"]):
                raise ValueError("auto_approve must be a bool")
            out["auto_approve"] = bounds["auto_approve"]
        if "quiet_hours_ist" in bounds:
            qh = bounds["quiet_hours_ist"]
            if qh is not None:
                if (not isinstance(qh, dict) or set(qh) != {"start", "end"}
                        or not _HHMM.match(qh["start"])
                        or not _HHMM.match(qh["end"])):
                    raise ValueError(
                        "quiet_hours_ist must be null or"
                        " {start: 'HH:MM', end: 'HH:MM'}")
                out["quiet_hours_ist"] = dict(qh)
            else:
                out["quiet_hours_ist"] = None
        if "min_score" in bounds:
            if not _is_int(bounds["min_score"]) or not 0 <= bounds["min_score"] <= 100:
                raise ValueError("min_score must be an int 0-100")
            out["min_score"] = bounds["min_score"]
        return out

    _standin = _stdlib_types.ModuleType("server.missions.types")
    _standin.MissionTypeSpec = MissionTypeSpec
    _standin.MISSION_TYPES = {
        "job_hunt": MissionTypeSpec(
            type="job_hunt", label="Job hunt",
            config_schema={
                "sources": (list, False, ["gmail"], None),
                "min_score": (int, False, 60, None),
                "roles": (list, False, [], None),
                "watch_new_only": (bool, False, True, None),
            },
            default_bounds=_default_bounds_for("job_hunt")),
        "cold_mail": MissionTypeSpec(
            type="cold_mail", label="Cold mail",
            config_schema={
                "role_ids": (list, False, [], None),
                "recipients": (list, False, [], None),
                "attach_resume": (bool, False, False, None),
                "max_drafts_per_run": (int, False, 5, None),
            },
            default_bounds=_default_bounds_for("cold_mail")),
        "price_watch": MissionTypeSpec(
            type="price_watch", label="Price watch",
            config_schema={
                "url": (str, True, None, None),
                "selector": (str, True, None, None),
                "condition": (str, False, "changed", None),
                "target_price": (int, False, None, None),
                "currency": (str, False, "INR", None),
                "one_shot": (bool, False, True, None),
            },
            default_bounds=_default_bounds_for("price_watch")),
    }
    _standin.validate_config = validate_config
    _standin.validate_bounds = validate_bounds
    sys.modules["server.missions.types"] = _standin
    import server.missions as _missions_pkg

    _missions_pkg.types = _standin
# --- end of types.py stand-in ---

from server.core import config, db  # noqa: E402
from server.missions import bounds, links, models  # noqa: E402
from server.tasks import approvals, loop  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")


class BoundsTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig_data_dir = config.DATA_DIR
        config.DATA_DIR = Path(self.tmp.name)
        db.migrate()
        self.conn = db.connect()

    def tearDown(self):
        self.conn.close()
        config.DATA_DIR = self._orig_data_dir
        self.tmp.cleanup()

    def _create(self, type="job_hunt", name="Test mission",
                config=None, bounds=None):
        return models.create_mission(
            self.conn, type, name, config or {}, bounds or {})

    def _activity_at(self, mission_id, kind, created_at, detail=None):
        # Direct insert: record_activity always stamps "now", and
        # the IST-boundary tests need rows at chosen timestamps.
        activity_id = f"ma_{uuid.uuid4().hex[:12]}"
        self.conn.execute(
            "INSERT INTO mission_activity (id, mission_id, run_id, kind,"
            " detail, created_at) VALUES (?, ?, NULL, ?, ?, ?)",
            (activity_id, mission_id, kind,
             json.dumps(detail or {}), created_at),
        )
        self.conn.commit()
        return activity_id

    def _applies(self, mission_id, n, at=None):
        for _ in range(n):
            if at is None:
                models.record_activity(self.conn, mission_id, "applied",
                                       {"role_id": "role_1"})
            else:
                self._activity_at(mission_id, "applied", at)

    def _sends(self, mission_id, n, at=None):
        for _ in range(n):
            if at is None:
                models.record_activity(self.conn, mission_id, "sent",
                                       {"draft_id": "d_1"})
            else:
                self._activity_at(mission_id, "sent", at)

    def _activity_rows(self):
        return self.conn.execute(
            "SELECT id, mission_id, kind, detail, created_at"
            " FROM mission_activity ORDER BY id"
        ).fetchall()

    def _audit_count(self):
        return self.conn.execute(
            "SELECT COUNT(*) AS c FROM audit_log"
        ).fetchone()["c"]


class TestIstDay(BoundsTestBase):
    def test_ist_day_start_known_values(self):
        # 23:59 IST on the 23rd belongs to the 23rd; 00:01 IST on
        # the 24th belongs to the 24th.
        late_23 = int(datetime(2026, 11, 23, 23, 59, tzinfo=IST).timestamp())
        early_24 = int(datetime(2026, 11, 24, 0, 1, tzinfo=IST).timestamp())
        midnight_23 = int(datetime(2026, 11, 23, 0, 0, tzinfo=IST).timestamp())
        midnight_24 = int(datetime(2026, 11, 24, 0, 0, tzinfo=IST).timestamp())
        self.assertEqual(bounds.ist_day_start(late_23), midnight_23)
        self.assertEqual(bounds.ist_day_start(early_24), midnight_24)
        # The result is midnight IST, not midnight UTC.
        day = datetime.fromtimestamp(bounds.ist_day_start(late_23), tz=IST)
        self.assertEqual((day.hour, day.minute, day.second), (0, 0, 0))
        self.assertEqual((day.day, day.month), (23, 11))

    def test_ist_day_start_defaults_to_now(self):
        now = int(time.time())
        start = bounds.ist_day_start()
        self.assertLessEqual(start, now)
        self.assertGreater(start, now - 86400)


class TestCheckBounds(BoundsTestBase):
    def test_under_cap_allowed(self):
        mission = self._create()
        allowed, reason = bounds.check_bounds(self.conn, mission, "apply")
        self.assertTrue(allowed)
        self.assertIsNone(reason)
        allowed, reason = bounds.check_bounds(self.conn, mission, "send")
        # A job_hunt mission sends nothing by default.
        self.assertFalse(allowed)
        self.assertIn("max_sends_per_day", reason)

    def test_at_applies_cap_refused(self):
        mission = self._create(bounds={"max_applies_per_day": 2})
        self._applies(mission["id"], 1)
        self.assertTrue(
            bounds.check_bounds(self.conn, mission, "apply")[0])
        self._applies(mission["id"], 1)
        allowed, reason = bounds.check_bounds(self.conn, mission, "apply")
        self.assertFalse(allowed)
        self.assertIn("max_applies_per_day", reason)
        self.assertIn("2/2", reason)

    def test_at_sends_cap_refused(self):
        mission = self._create(
            type="cold_mail", bounds={"max_sends_per_day": 1})
        self._sends(mission["id"], 1)
        allowed, reason = bounds.check_bounds(self.conn, mission, "send")
        self.assertFalse(allowed)
        self.assertIn("max_sends_per_day", reason)

    def test_at_total_cap_refused(self):
        mission = self._create(bounds={
            "max_actions_per_day": 3, "max_applies_per_day": 3,
            "max_sends_per_day": 3})
        self._applies(mission["id"], 2)
        self._sends(mission["id"], 1)
        for action in ("apply", "send"):
            allowed, reason = bounds.check_bounds(
                self.conn, mission, action)
            self.assertFalse(allowed)
            self.assertIn("max_actions_per_day", reason)
            self.assertIn("3/3", reason)

    def test_zero_total_cap_refuses_everything(self):
        # The kind caps are raised so the TOTAL cap is the one
        # that refuses both kinds (a job_hunt mission's send cap
        # is 0 by default, which would refuse sends first).
        mission = self._create(bounds={
            "max_actions_per_day": 0, "max_applies_per_day": 3,
            "max_sends_per_day": 5})
        for action in ("apply", "send"):
            allowed, reason = bounds.check_bounds(
                self.conn, mission, action)
            self.assertFalse(allowed)
            self.assertIn("max_actions_per_day", reason)

    def test_zero_kind_cap_refuses_only_that_kind(self):
        mission = self._create(bounds={
            "max_applies_per_day": 0, "max_sends_per_day": 5,
            "max_actions_per_day": 5})
        allowed, reason = bounds.check_bounds(self.conn, mission, "apply")
        self.assertFalse(allowed)
        self.assertIn("max_applies_per_day", reason)
        self.assertTrue(
            bounds.check_bounds(self.conn, mission, "send")[0])

    def test_ist_boundary_fixed_timestamps(self):
        # 23:59 IST on the 23rd vs 00:01 IST on the 24th — the
        # day boundary is IST midnight, not UTC midnight.
        late_23 = int(datetime(2026, 11, 23, 23, 59, tzinfo=IST).timestamp())
        early_24 = int(datetime(2026, 11, 24, 0, 1, tzinfo=IST).timestamp())
        morning_24 = int(datetime(2026, 11, 24, 10, 0, tzinfo=IST).timestamp())
        mission = self._create(bounds={"max_applies_per_day": 1})
        self._activity_at(mission["id"], "applied", late_23)
        # The 23rd's apply does not count on the 24th.
        self.assertTrue(
            bounds.check_bounds(self.conn, mission, "apply",
                                now=morning_24)[0])
        self._activity_at(mission["id"], "applied", early_24)
        # The 24th's 00:01 apply counts.
        allowed, reason = bounds.check_bounds(
            self.conn, mission, "apply", now=morning_24)
        self.assertFalse(allowed)
        self.assertIn("max_applies_per_day", reason)

    def test_yesterday_actions_do_not_count_today(self):
        day_start = bounds.ist_day_start()
        mission = self._create(bounds={"max_applies_per_day": 1})
        # 23:59 IST yesterday.
        self._activity_at(mission["id"], "applied", day_start - 60)
        self.assertTrue(
            bounds.check_bounds(self.conn, mission, "apply")[0])
        # 00:01 IST today.
        self._activity_at(mission["id"], "applied", day_start + 60)
        self.assertFalse(
            bounds.check_bounds(self.conn, mission, "apply")[0])

    def test_other_missions_activity_does_not_count(self):
        first = self._create(name="First")
        second = self._create(name="Second")
        self._applies(first["id"], 3)
        self.assertFalse(
            bounds.check_bounds(self.conn, first, "apply")[0])
        self.assertTrue(
            bounds.check_bounds(self.conn, second, "apply")[0])

    def test_non_action_activity_does_not_count(self):
        mission = self._create()
        for _ in range(5):
            models.record_activity(self.conn, mission["id"], "discovered",
                                   {"role_id": "role_1"})
            models.record_activity(self.conn, mission["id"], "refused",
                                   {"reason": "test"})
        self.assertTrue(
            bounds.check_bounds(self.conn, mission, "apply")[0])

    def test_check_bounds_is_pure(self):
        mission = self._create()
        self._applies(mission["id"], 1)
        before_activity = self._activity_rows()
        before_audit = self._audit_count()
        bounds.check_bounds(self.conn, mission, "apply")
        bounds.check_bounds(self.conn, mission, "send")
        bounds.auto_approvable(self.conn, mission, "apply")
        self.assertEqual(self._activity_rows(), before_activity)
        self.assertEqual(self._audit_count(), before_audit)

    def test_unknown_action_raises(self):
        mission = self._create()
        with self.assertRaises(ValueError):
            bounds.check_bounds(self.conn, mission, "teleport")


class TestAutoApprovable(BoundsTestBase):
    """The Q1 override: standing approval auto-satisfies a pending
    approval only when auto_approve is on, the kind is out of
    require_approval_for, AND the action is within bounds. All
    four combinations, plus the end-to-end auto-satisfy path."""

    def test_auto_on_within_bounds(self):
        mission = self._create(bounds={
            "auto_approve": True, "require_approval_for": []})
        allowed, reason = bounds.auto_approvable(
            self.conn, mission, "apply")
        self.assertTrue(allowed)
        self.assertIsNone(reason)

    def test_auto_on_kind_still_requires_approval(self):
        mission = self._create(bounds={
            "auto_approve": True, "require_approval_for": ["apply"],
            "max_sends_per_day": 5})
        allowed, reason = bounds.auto_approvable(
            self.conn, mission, "apply")
        self.assertFalse(allowed)
        self.assertEqual(reason, "requires_human_approval")
        # ...but the other kind is covered (a job_hunt mission
        # sends nothing by default, so the send cap is raised
        # above for this check).
        self.assertTrue(
            bounds.auto_approvable(self.conn, mission, "send")[0])

    def test_auto_off_within_bounds(self):
        mission = self._create(bounds={
            "auto_approve": False, "require_approval_for": []})
        allowed, reason = bounds.auto_approvable(
            self.conn, mission, "apply")
        self.assertFalse(allowed)
        self.assertEqual(reason, "auto_approve_off")

    def test_auto_on_over_cap(self):
        mission = self._create(bounds={
            "auto_approve": True, "require_approval_for": [],
            "max_applies_per_day": 1})
        self._applies(mission["id"], 1)
        allowed, reason = bounds.auto_approvable(
            self.conn, mission, "apply")
        self.assertFalse(allowed)
        self.assertIn("max_applies_per_day", reason)

    def test_auto_approve_true_is_allowed_at_creation(self):
        # Q1 override: the design doc's 422 gate on
        # auto_approve:true is NOT implemented — standing
        # approval is part of PR #5.
        mission = self._create(bounds={
            "auto_approve": True, "require_approval_for": ["send"]})
        self.assertTrue(mission["bounds"]["auto_approve"])
        self.assertEqual(mission["bounds"]["require_approval_for"],
                         ["send"])

    def test_auto_satisfy_end_to_end(self):
        # The runner's path: record pending -> auto_approvable ->
        # approve_pending (the shared gate) -> the grant unblocks
        # the submit.
        mission = self._create(bounds={
            "auto_approve": True, "require_approval_for": []})
        models.transition(self.conn, mission["id"], "active")
        mission = models.get_mission(self.conn, mission["id"])
        task_id = loop.create_task(self.conn, "Apply to role_1")
        links.set_task_mission(self.conn, task_id, mission["id"])
        approval_id = loop.record_pending_approval(
            self.conn, task_id, step=1, action="submit",
            element_id="el_submit", role_id="role_1")
        allowed, reason = bounds.auto_approvable(
            self.conn, mission, "apply")
        self.assertTrue(allowed)
        result = approvals.approve_pending(
            self.conn, task_id, approval_id)
        self.assertEqual(result["approval_id"], approval_id)
        self.assertIsNone(
            loop.get_pending_approval(self.conn, approval_id))
        # The minted grant is scoped to the element/role, and the
        # loop consumes it for the submit.
        self.assertTrue(loop._consume_approval(
            self.conn, task_id, element_id="el_submit",
            role_id="role_1"))

    def test_auto_satisfy_refused_over_cap_mints_no_grant(self):
        mission = self._create(bounds={
            "auto_approve": True, "require_approval_for": [],
            "max_applies_per_day": 1})
        self._applies(mission["id"], 1)
        task_id = loop.create_task(self.conn, "Apply to role_1")
        links.set_task_mission(self.conn, task_id, mission["id"])
        approval_id = loop.record_pending_approval(
            self.conn, task_id, step=1, action="submit")
        allowed, reason = bounds.auto_approvable(
            self.conn, mission, "apply")
        self.assertFalse(allowed)
        # The runner refuses instead of auto-satisfying: no grant
        # is minted and the pending approval is left alone.
        self.assertIsNotNone(
            loop.get_pending_approval(self.conn, approval_id))
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) AS c FROM grants WHERE origin = ?",
                (task_id,)).fetchone()["c"], 0)

    def test_auto_satisfy_left_pending_when_kind_requires_approval(self):
        mission = self._create(bounds={
            "auto_approve": True, "require_approval_for": ["apply"]})
        task_id = loop.create_task(self.conn, "Apply to role_1")
        links.set_task_mission(self.conn, task_id, mission["id"])
        approval_id = loop.record_pending_approval(
            self.conn, task_id, step=1, action="submit")
        allowed, reason = bounds.auto_approvable(
            self.conn, mission, "apply")
        self.assertFalse(allowed)
        self.assertEqual(reason, "requires_human_approval")
        # Out of bounds for auto-approval: the pending approval
        # stays for the user — the runner does not consume it.
        self.assertIsNotNone(
            loop.get_pending_approval(self.conn, approval_id))


class TestDefaultBounds(BoundsTestBase):
    def test_default_bounds_per_type(self):
        job = bounds.default_bounds("job_hunt")
        self.assertEqual(job["max_actions_per_day"], 5)
        self.assertEqual(job["max_applies_per_day"], 3)
        self.assertEqual(job["max_sends_per_day"], 0)
        self.assertFalse(job["auto_approve"])
        self.assertEqual(job["require_approval_for"], ["apply", "send"])
        cold = bounds.default_bounds("cold_mail")
        self.assertEqual(cold["max_applies_per_day"], 0)
        self.assertEqual(cold["max_sends_per_day"], 5)
        price = bounds.default_bounds("price_watch")
        self.assertEqual(price["max_applies_per_day"], 0)
        self.assertEqual(price["max_sends_per_day"], 0)
        # A copy, not the registry's own dict — mutating it must
        # not change the defaults for the next caller.
        job["max_actions_per_day"] = 99
        self.assertEqual(
            bounds.default_bounds("job_hunt")["max_actions_per_day"], 5)

    def test_default_bounds_unknown_type_raises(self):
        with self.assertRaises(ValueError):
            bounds.default_bounds("time_travel")


if __name__ == "__main__":
    unittest.main()
