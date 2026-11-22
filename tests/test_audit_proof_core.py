"""Audit + proof + core regression tests.

Covers the hash-chained audit log (tamper detection, export
completeness), DB migrations, token file permissions, the proof
API honesty strings, score.py labelling, the harness fail-loud
rules, and the benchmark's plan-failure counting and live
outbound-call capture.

Every test here failed on the pre-fix code (or pins behaviour the
audit proved correct) and passes after the fix.
"""
import json
import os
import re
import stat
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "evals" / "sih-benchmark"))

from server.api import audit_api  # noqa: E402
from server.api import proof_api  # noqa: E402
from server.core import audit, config, db  # noqa: E402
from server.proof import clear as clear_outbound  # noqa: E402
from server.proof import record as record_outbound  # noqa: E402
from server.tasks import loop  # noqa: E402

import benchmark as bench  # noqa: E402
import run as run_mod  # noqa: E402
import score as score_mod  # noqa: E402
from server.planner import router as planner_router  # noqa: E402


def _seed_audit(conn, n=3):
    for i in range(n):
        audit.append(conn, f"task-{i}", "step_done", {"n": i})


class TestHashChain(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="eclipse-audit-")
        self.db_path = Path(self.tmp.name) / "eclipse.db"
        db.migrate(self.db_path)
        self.conn = db.connect(self.db_path)
        _seed_audit(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_clean_chain_verifies(self):
        result = audit.verify(self.conn)
        self.assertTrue(result["ok"])
        self.assertEqual(result["checked"], 3)

    def test_tampered_action_is_caught(self):
        self.conn.execute("UPDATE audit_log SET action='forged' WHERE id=2")
        self.conn.commit()
        result = audit.verify(self.conn)
        self.assertFalse(result["ok"])
        self.assertEqual(result["first_bad_id"], 2)
        self.assertEqual(result["checked"], 1)

    def test_tampered_payload_hash_is_caught(self):
        self.conn.execute(
            "UPDATE audit_log SET payload_hash=? WHERE id=2", ("0" * 64,)
        )
        self.conn.commit()
        result = audit.verify(self.conn)
        self.assertFalse(result["ok"])
        self.assertEqual(result["first_bad_id"], 2)

    def test_tampered_prev_hash_is_caught(self):
        self.conn.execute(
            "UPDATE audit_log SET prev_hash=? WHERE id=3", ("0" * 64,)
        )
        self.conn.commit()
        result = audit.verify(self.conn)
        self.assertFalse(result["ok"])
        self.assertEqual(result["first_bad_id"], 3)
        self.assertIn("prev_hash", result["reason"])


class TestAuditApiCompleteness(unittest.TestCase):
    """list/export must return every entry, not a page of them."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="eclipse-audit-api-")
        self._old_data_dir = config.DATA_DIR
        config.DATA_DIR = Path(self.tmp.name)
        db.migrate()
        conn = db.connect()
        try:
            _seed_audit(conn, n=5)
        finally:
            conn.close()

    def tearDown(self):
        config.DATA_DIR = self._old_data_dir
        self.tmp.cleanup()

    def test_list_returns_all_entries(self):
        # task_id=None passed explicitly: the Query(...) default is
        # only resolved by FastAPI, not on a direct call.
        doc = audit_api.list_audit(task_id=None)
        self.assertEqual(len(doc["entries"]), 5)
        self.assertEqual([e["id"] for e in doc["entries"]], [1, 2, 3, 4, 5])

    def test_export_returns_all_entries(self):
        doc = audit_api.export_audit()
        self.assertEqual(len(doc["entries"]), 5)
        self.assertIn("exported_at", doc)


class TestMigrations(unittest.TestCase):
    def test_migrate_is_idempotent(self):
        with tempfile.TemporaryDirectory(prefix="eclipse-migrate-") as tmp:
            path = Path(tmp) / "eclipse.db"
            db.migrate(path)
            db.migrate(path)
            db.migrate(path)
            conn = db.connect(path)
            try:
                quota = conn.execute(
                    "SELECT COUNT(*) FROM quota"
                ).fetchone()[0]
                connectors = conn.execute(
                    "SELECT COUNT(*) FROM connectors"
                ).fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(quota, 5)
            self.assertEqual(connectors, 3)

    def test_migrate_upgrades_old_db_without_losing_data(self):
        import sqlite3

        with tempfile.TemporaryDirectory(prefix="eclipse-migrate-old-") as tmp:
            path = Path(tmp) / "old.db"
            c = sqlite3.connect(path)
            c.executescript(
                "CREATE TABLE facts(id TEXT PRIMARY KEY, text TEXT,"
                " source TEXT, created_at INTEGER);"
                "CREATE TABLE watches(id TEXT PRIMARY KEY, url TEXT);"
                "INSERT INTO facts VALUES ('f1','hello','t',1);"
            )
            c.commit()
            c.close()
            db.migrate(path)
            conn = db.connect(path)
            try:
                cols = {r[1] for r in conn.execute("PRAGMA table_info(facts)")}
                text = conn.execute(
                    "SELECT text FROM facts WHERE id='f1'"
                ).fetchone()[0]
                has_watch_events = conn.execute(
                    "SELECT COUNT(*) FROM sqlite_master"
                    " WHERE name='watch_events'"
                ).fetchone()[0]
            finally:
                conn.close()
            self.assertIn("topic", cols)
            self.assertEqual(text, "hello")
            self.assertEqual(has_watch_events, 1)


class TestTokenFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="eclipse-token-")
        self._old_data_dir = config.DATA_DIR
        self._old_token = config._token
        self._old_env = os.environ.get("ECLIPSE_TOKEN")
        os.environ.pop("ECLIPSE_TOKEN", None)
        config.DATA_DIR = Path(self.tmp.name)
        config._token = None

    def tearDown(self):
        config.DATA_DIR = self._old_data_dir
        config._token = self._old_token
        if self._old_env is None:
            os.environ.pop("ECLIPSE_TOKEN", None)
        else:
            os.environ["ECLIPSE_TOKEN"] = self._old_env
        self.tmp.cleanup()

    def _mode(self):
        return stat.S_IMODE(os.stat(config.TOKEN_FILE).st_mode)

    def test_fresh_token_file_is_0600(self):
        config.get_token()
        self.assertEqual(self._mode(), 0o600)

    def test_lax_existing_file_is_tightened(self):
        first = config.get_token()
        os.chmod(config.TOKEN_FILE, 0o644)
        config._token = None
        second = config.get_token()
        self.assertEqual(second, first)
        self.assertEqual(self._mode(), 0o600)


def _live_benchmark_doc(**overrides):
    doc = {
        "benchmark": "eclipse-sih",
        "sprint": 6,
        "generated_at": "2026-11-22T10:00:00",
        "fixtures": 12,
        "metrics": {
            "visual_grounding_first_try": 0.789,
            "pii_recall": 1.0,
            "pii_precision": 1.0,
            "redaction_recoverable_tokens": 0,
            "resource_mb": 26.7,
            "latency_p50_s": 2.64,
        },
        "detail": {
            "pii": {"recall": 1.0, "precision": 1.0, "worst_fixture": None},
            "loop": {
                "grounding_first_try": 0.789,
                "human_only_handoffs": 2,
                "worst_fixture": "job-listing",
                "planner": (
                    "live google-gemma-4-31b-it (own Antseed gateway, "
                    "surrogate auth) — real model grounding and latency"
                ),
            },
        },
        "honesty": "live run",
    }
    doc.update(overrides)
    return doc


class TestScoreLabelling(unittest.TestCase):
    def _score_doc(self, bench_doc):
        with tempfile.TemporaryDirectory(prefix="eclipse-score-") as tmp:
            path = Path(tmp) / "benchmark-live-20261122-100000.json"
            path.write_text(json.dumps(bench_doc), encoding="utf-8")
            return score_mod.score(path)

    def test_live_run_is_not_labelled_mock(self):
        doc = self._score_doc(_live_benchmark_doc())
        grounding = doc["metrics"]["visual_grounding_first_try"]
        latency = doc["metrics"]["latency_p50_s"]
        self.assertNotIn("mock", grounding["note"])
        self.assertIn("live", grounding["note"])
        self.assertNotIn("mock", latency["note"])
        self.assertIn("live", latency["note"])

    def test_mock_run_keeps_its_mock_label(self):
        bench_doc = _live_benchmark_doc()
        bench_doc["detail"]["loop"]["planner"] = (
            "mock (ground-truth actions) — NOT live gateway latency"
        )
        doc = self._score_doc(bench_doc)
        self.assertIn("mock", doc["metrics"]["visual_grounding_first_try"]["note"])
        self.assertIn("mock", doc["metrics"]["latency_p50_s"]["note"])

    def test_missing_metric_is_not_measured_not_measured(self):
        bench_doc = _live_benchmark_doc()
        del bench_doc["metrics"]["latency_p50_s"]
        doc = self._score_doc(bench_doc)
        latency = doc["metrics"]["latency_p50_s"]
        self.assertIsNone(latency["value"])
        self.assertEqual(latency["status"], "not_measured")


class TestProofApiNotes(unittest.TestCase):
    def setUp(self):
        self._old_latest = proof_api._latest_benchmark
        clear_outbound()

    def tearDown(self):
        proof_api._latest_benchmark = self._old_latest
        clear_outbound()

    def test_redaction_note_reports_actual_recoverable_tokens(self):
        proof_api._latest_benchmark = lambda: {
            "generated_at": "2026-11-22T10:00:00",
            "detail": {
                "redaction": {
                    "recoverable_tokens": 3,
                    "passed": False,
                    "per_fixture": {},
                },
                "pii": {"recall": 1.0, "precision": 1.0, "per_fixture": {}},
            },
        }
        doc = proof_api.redaction()
        self.assertEqual(doc["recoverable_tokens"], 3)
        self.assertFalse(doc["passed"])
        self.assertIn("3", doc["note"])
        self.assertNotIn("found 0", doc["note"])

    def test_redaction_note_still_says_zero_when_zero(self):
        proof_api._latest_benchmark = lambda: {
            "generated_at": "2026-11-22T10:00:00",
            "detail": {
                "redaction": {
                    "recoverable_tokens": 0,
                    "passed": True,
                    "per_fixture": {},
                },
                "pii": {"recall": 1.0, "precision": 1.0, "per_fixture": {}},
            },
        }
        doc = proof_api.redaction()
        self.assertIn("found 0", doc["note"])

    def test_latency_note_matches_a_live_planner(self):
        proof_api._latest_benchmark = lambda: {
            "generated_at": "2026-11-22T10:00:00",
            "detail": {
                "loop": {
                    "step_time_s": {"p50": 2.64, "max": 5.1, "mean": 2.8},
                    "latency_target_s": 6,
                    "p50_within_target": True,
                    "worst_fixture": "job-listing",
                    "worst_fixture_s": 9.4,
                    "per_fixture": {},
                    "planner": (
                        "live google-gemma-4-31b-it (own Antseed gateway, "
                        "surrogate auth) — real model grounding and latency"
                    ),
                },
            },
        }
        doc = proof_api.latency()
        self.assertNotIn("mock planner", doc["note"])
        self.assertIn("live gateway run", doc["note"])

    def test_latency_note_matches_a_mock_planner(self):
        proof_api._latest_benchmark = lambda: {
            "generated_at": "2026-11-22T10:00:00",
            "detail": {
                "loop": {
                    "step_time_s": {"p50": 0.002, "max": 0.5, "mean": 0.04},
                    "latency_target_s": 6,
                    "p50_within_target": True,
                    "worst_fixture": None,
                    "worst_fixture_s": 0.5,
                    "per_fixture": {},
                    "planner": "mock (ground-truth actions) — NOT live gateway latency",
                },
            },
        }
        doc = proof_api.latency()
        self.assertIn("mock planner", doc["note"])

    def test_egress_says_zero_checked_when_no_call_was_recorded(self):
        doc = proof_api.egress()
        self.assertEqual(doc["checked"], 0)
        self.assertIn("scope", doc)
        self.assertIn("0 calls checked", doc["note"])
        self.assertIn("this server", doc["note"])

    def test_what_was_sent_states_its_scope(self):
        doc = proof_api.what_was_sent(q="", limit=50)
        self.assertEqual(doc["count"], 0)
        self.assertIn("scope", doc)
        self.assertIn("benchmark harness", doc["scope"])


class TestHarnessFailsLoud(unittest.TestCase):
    def setUp(self):
        self._old_gt = run_mod.GROUND_TRUTH

    def tearDown(self):
        run_mod.GROUND_TRUTH = self._old_gt

    def test_empty_fixture_set_raises(self):
        with tempfile.TemporaryDirectory(prefix="eclipse-run-empty-") as tmp:
            gt = Path(tmp) / "ground_truth.json"
            gt.write_text(json.dumps({"fixtures": {}}), encoding="utf-8")
            run_mod.GROUND_TRUTH = gt
            with self.assertRaises(ValueError):
                run_mod.run(1, Path(tmp) / "results")

    def test_runs_zero_raises(self):
        with tempfile.TemporaryDirectory(prefix="eclipse-run-zero-") as tmp:
            with self.assertRaises(ValueError):
                run_mod.run(0, Path(tmp) / "results")

    def test_main_returns_2_on_empty_fixtures(self):
        with tempfile.TemporaryDirectory(prefix="eclipse-run-main-") as tmp:
            gt = Path(tmp) / "ground_truth.json"
            gt.write_text(json.dumps({"fixtures": {}}), encoding="utf-8")
            run_mod.GROUND_TRUTH = gt
            code = run_mod.main(
                ["--runs", "1", "--out", str(Path(tmp) / "results")]
            )
            self.assertEqual(code, 2)


class TestPiiMath(unittest.TestCase):
    """Hand-computed check of benchmark._pii_metrics.

    Regex-detectable expected entries per fixture:
    application-form 2 (email, phone), checkout 1 (card_number),
    hidden-fields 2 (email x2), inbox 3 (email x3), login 1
    (email), pii-text 5 (email, phone, aadhaar, pan, dob) = 14.
    Names, addresses, CVV, OTP and passwords are label-based and
    must not count as regex misses.
    """

    def test_totals_match_hand_computation(self):
        gt = bench._load_ground_truth()
        pii = bench._pii_metrics(gt)
        self.assertEqual(pii["true_positives"], 14)
        self.assertEqual(pii["false_positives"], 0)
        self.assertEqual(pii["false_negatives"], 0)
        self.assertEqual(pii["recall"], 1.0)
        self.assertEqual(pii["precision"], 1.0)

    def test_login_and_checkout_per_fixture(self):
        gt = bench._load_ground_truth()
        pii = bench._pii_metrics(gt)
        login = pii["per_fixture"]["login"]
        self.assertEqual(login["expected_regex"], 1)
        self.assertEqual(login["found"], 1)
        self.assertEqual(login["label_based_not_regex"], ["el_login_password"])
        checkout = pii["per_fixture"]["checkout"]
        self.assertEqual(checkout["expected_regex"], 1)
        self.assertEqual(checkout["found"], 1)
        self.assertEqual(
            sorted(checkout["label_based_not_regex"]),
            ["el_card_cvv", "el_card_name"],
        )


class TestBenchmarkPlanFailure(unittest.TestCase):
    def test_plan_failure_counts_against_grounding(self):
        gt = bench._load_ground_truth()
        orig_plan = loop.plan_task
        calls = {"n": 0}

        def flaky_plan(conn, task_id, call_fn=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("simulated plan failure")
            return orig_plan(conn, task_id, call_fn=call_fn)

        loop.plan_task = flaky_plan
        try:
            m = bench._loop_metrics(gt, live=False)
        finally:
            loop.plan_task = orig_plan
        # application-form (5 steps) failed to plan: those 5 steps
        # are failures, not absences — the old code dropped them
        # and still reported grounding 1.0.
        self.assertEqual(m["total_steps"], 20)
        self.assertEqual(m["fixture_errors"], 1)
        self.assertLess(m["grounding_first_try"], 1.0)
        self.assertIn("error", m["per_fixture"]["application-form"])

    def test_main_exits_1_when_a_fixture_errors(self):
        gt = bench._load_ground_truth()
        orig_plan = loop.plan_task
        calls = {"n": 0}

        def flaky_plan(conn, task_id, call_fn=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("simulated plan failure")
            return orig_plan(conn, task_id, call_fn=call_fn)

        loop.plan_task = flaky_plan
        old_argv = sys.argv
        try:
            with tempfile.TemporaryDirectory(prefix="eclipse-bench-main-") as tmp:
                sys.argv = [
                    "benchmark.py", "--out", str(Path(tmp) / "b.json")
                ]
                code = bench.main()
            self.assertEqual(code, 1)
        finally:
            loop.plan_task = orig_plan
            sys.argv = old_argv


class TestLiveOutboundCapture(unittest.TestCase):
    """A live run's planner calls must be counted in its results
    file — the harness process's outbound log dies with the
    process, so capturing it is the only record those calls
    happened."""

    def test_live_run_captures_its_planner_calls(self):
        gt = bench._load_ground_truth()
        fns: dict[str, tuple] = {}

        def recording_stub(prompt, local_only=False):
            # Stand in for the real router.call_planner, which
            # records every call it makes in server.proof.
            record_outbound(
                "https://example.test/v1/chat/completions",
                "stub-model",
                prompt,
                1.0,
                True,
            )
            name = re.search(r"GOAL: benchmark (\S+)", prompt).group(1)
            if name not in fns:
                actions = [
                    a
                    for a in gt["fixtures"][name]["target_actions"]
                    if a["action"] in ("click", "type", "select")
                ]
                fns[name] = (
                    bench._mock_plan_fn(actions),
                    bench._mock_step_fn(actions),
                )
            plan_fn, step_fn = fns[name]
            if "Decompose the goal into a plan" in prompt:
                return plan_fn(prompt)
            return step_fn(prompt)

        orig_call = planner_router.call_planner
        planner_router.call_planner = recording_stub
        try:
            m = bench._loop_metrics(gt, live=True)
        finally:
            planner_router.call_planner = orig_call
            clear_outbound()
        # 5 fixtures with actions x 1 plan call + 20 step calls,
        # minus the 1 step the loop refuses as human-only BEFORE
        # any planner call (checkout step 3, OTP — loop.py raises
        # on the plan description before calling the planner).
        self.assertEqual(len(m["outbound_calls"]), 24)
        self.assertTrue(
            all(c["ok"] for c in m["outbound_calls"]),
            "all stubbed calls succeeded",
        )
        self.assertEqual(m["grounding_first_try"], 1.0)

    def test_mock_run_records_no_outbound_calls(self):
        gt = bench._load_ground_truth()
        m = bench._loop_metrics(gt, live=False)
        self.assertEqual(m["outbound_calls"], [])


if __name__ == "__main__":
    unittest.main()
