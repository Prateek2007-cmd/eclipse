"""Task-loop tests (Sprint 3): the server loop on a fixture-style
observation, with a MOCK planner (deterministic — no API key, no
live model), plus the task API against a live server subprocess.

The loop test runs the login fixture's 3 target actions end to end:
type email → type password → click submit, verifying each step by
re-observing.
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from server.core import config, db  # noqa: E402
from server.planner import validate  # noqa: E402
from server.tasks import loop  # noqa: E402

TOKEN = "test-token-tasks"

LOGIN_OBS = {
    "url": "http://localhost/login.html",
    "elements": [
        {"id": "el_login_email", "tag": "input", "role": "textbox",
         "name": "Email", "text": "", "value": "", "visible": True, "interactable": True},
        {"id": "el_login_password", "tag": "input", "role": "textbox",
         "name": "Password", "text": "", "value": "", "visible": True, "interactable": True},
        {"id": "el_login_submit", "tag": "button", "role": "button",
         "name": "Log in", "text": "", "visible": True, "interactable": True},
    ],
}


def _mock_plan(prompt: str, **kwargs):
    return (
        json.dumps([
            {"step": 1, "description": "type the email into the email field",
             "expected_end_state": "email field holds a value"},
            {"step": 2, "description": "type the password into the password field",
             "expected_end_state": "password field holds a value"},
            {"step": 3, "description": "click the log in button",
             "expected_end_state": "application form opens"},
        ]),
        "mock:planner",
        5.0,
    )


def _mock_action_for(step_n: int):
    actions = {
        1: {"action": "type", "element_id": "el_login_email", "text": "[EMAIL_1]",
            "reason": "fill the email"},
        2: {"action": "type", "element_id": "el_login_password", "text": "[PASSWORD_1]",
            "reason": "fill the password"},
        3: {"action": "click", "element_id": "el_login_submit",
            "reason": "submit the login form"},
    }
    return lambda prompt, **kwargs: (json.dumps(actions[step_n]), "mock:planner", 5.0)


class TestTaskLoop(unittest.TestCase):
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

    def _obs_with_value(self, element_id: str, value: str) -> dict:
        obs = json.loads(json.dumps(LOGIN_OBS))
        for el in obs["elements"]:
            if el["id"] == element_id:
                el["value"] = value
        return obs

    def test_login_flow_end_to_end(self):
        # The login needs the vault: the password is a secret
        # placeholder, so the vault must be unlocked for the task
        # (with the password stored for this origin) and an
        # approval grant must exist — otherwise execute_step asks
        # the user for approval instead of filling (B3).
        from server.api.vault_api import VAULT
        from server.vault import grants as grant_store

        VAULT.create("correct horse battery staple")
        VAULT.store_password("http://localhost", "password", "hunter2")
        task_id = loop.create_task(
            self.conn, "Log in to the test portal", "allowed sites: localhost"
        )
        grant_store.GRANTS.create(task_id)
        import time as _time

        self.conn.execute(
            "INSERT INTO grants (id, origin, scope, expires_at, created_at)"
            " VALUES (?, ?, 'approve', ?, ?)",
            (uuid.uuid4().hex[:12], task_id, int(_time.time()) + 600,
             int(_time.time())),
        )
        self.conn.commit()
        try:
            steps = loop.plan_task(self.conn, task_id, call_fn=_mock_plan)
            self.assertEqual(len(steps), 3)

            # Step 1: type the email, then verify by re-observing.
            # The baseline is the pre-action observation (empty field).
            r1 = loop.execute_step(
                self.conn, task_id, 1, LOGIN_OBS, call_fn=_mock_action_for(1)
            )
            self.assertEqual(r1["action"]["element_id"], "el_login_email")
            v1 = loop.verify_step(
                self.conn, task_id, 1,
                self._obs_with_value("el_login_email", "[EMAIL_1]"),
                r1["expected_end_state"], previous_url=LOGIN_OBS["url"],
                previous_observation=LOGIN_OBS,
            )
            self.assertTrue(v1["verified"], v1)

            # Step 2: type the password, verify. The fill map
            # carries the stored password for the password field.
            r2 = loop.execute_step(
                self.conn, task_id, 2, LOGIN_OBS, call_fn=_mock_action_for(2)
            )
            self.assertEqual(r2.get("fill", {}).get("[PASSWORD_1]"), "hunter2", r2)
            v2 = loop.verify_step(
                self.conn, task_id, 2,
                self._obs_with_value("el_login_password", "[PASSWORD_1]"),
                r2["expected_end_state"], previous_url=LOGIN_OBS["url"],
                previous_observation=LOGIN_OBS,
            )
            self.assertTrue(v2["verified"], v2)

            # Step 3: click submit, verify by the URL changing.
            r3 = loop.execute_step(
                self.conn, task_id, 3, LOGIN_OBS, call_fn=_mock_action_for(3)
            )
            new_obs = {"url": "http://localhost/application-form.html", "elements": []}
            v3 = loop.verify_step(
                self.conn, task_id, 3, new_obs,
                r3["expected_end_state"], previous_url=LOGIN_OBS["url"],
                previous_observation=LOGIN_OBS,
            )
            self.assertTrue(v3["verified"], v3)
        finally:
            VAULT.lock()
            grant_store.GRANTS.revoke_all()

        task = loop.get_task(self.conn, task_id)
        self.assertEqual(task["status"], "done")
        self.assertTrue(loop.checkpoint_summary(self.conn, task_id))
        # One checkpoint per verified step.
        rows = self.conn.execute(
            "SELECT COUNT(*) AS c FROM checkpoints WHERE task_id = ?", (task_id,)
        ).fetchone()
        self.assertEqual(rows["c"], 3)

    def test_invalid_planner_output_retried_then_ask_user(self):
        task_id = loop.create_task(self.conn, "Log in to the test portal")
        loop.plan_task(self.conn, task_id, call_fn=_mock_plan)

        def bad_planner(prompt, **kwargs):
            # Names an element that is not in the observation.
            return (json.dumps({"action": "click", "element_id": "el_nope"}),
                    "mock:planner", 5.0)

        # A grounding failure raises GroundingError (re-observe and
        # re-plan), not a generic validation error.
        with self.assertRaises(loop.GroundingError):
            loop.execute_step(self.conn, task_id, 1, LOGIN_OBS, call_fn=bad_planner)
        # A second grounding failure in a row asks the user.
        with self.assertRaises(loop.GroundingError):
            loop.execute_step(self.conn, task_id, 1, LOGIN_OBS, call_fn=bad_planner)
        task = loop.get_task(self.conn, task_id)
        self.assertEqual(task["status"], "ask_user")

    def test_human_only_step_hands_off(self):
        def captcha_plan(prompt, **kwargs):
            return (json.dumps([
                {"step": 1, "description": "solve the CAPTCHA box",
                 "expected_end_state": "captcha passed"},
            ]), "mock:planner", 5.0)

        task_id = loop.create_task(self.conn, "Open the captcha page")
        loop.plan_task(self.conn, task_id, call_fn=captcha_plan)
        with self.assertRaises(ValueError):
            loop.execute_step(self.conn, task_id, 1, LOGIN_OBS, call_fn=_mock_action_for(1))
        self.assertEqual(loop.get_task(self.conn, task_id)["status"], "handoff")
        # The handoff saved a checkpoint and an audit entry.
        self.assertTrue(loop.checkpoint_summary(self.conn, task_id))
        rows = self.conn.execute(
            "SELECT COUNT(*) AS c FROM audit_log WHERE task_id = ? AND action = 'task.handoff'",
            (task_id,),
        ).fetchone()
        self.assertEqual(rows["c"], 1)
        # Resume: the user completed the human step, the task
        # continues (the handoff step is marked done).
        status = loop.resume_task(self.conn, task_id)
        self.assertEqual(status, "planned")
        self.assertEqual(loop.get_task(self.conn, task_id)["status"], "planned")

    def test_verify_retries_then_fails(self):
        task_id = loop.create_task(self.conn, "Log in to the test portal")
        loop.plan_task(self.conn, task_id, call_fn=_mock_plan)
        loop.execute_step(self.conn, task_id, 1, LOGIN_OBS, call_fn=_mock_action_for(1))
        # The observation never changes: the field stays empty.
        for attempt in range(3):
            result = loop.verify_step(
                self.conn, task_id, 1, LOGIN_OBS,
                "email field holds a value", previous_url=LOGIN_OBS["url"],
                previous_observation=LOGIN_OBS,
            )
            if attempt < 2:
                self.assertFalse(result["verified"])
                self.assertTrue(result["retry"])
            else:
                self.assertFalse(result["verified"])
                self.assertFalse(result["retry"])
        self.assertEqual(loop.get_task(self.conn, task_id)["status"], "failed")

    def test_verify_does_not_pass_when_the_action_did_nothing(self):
        # Review probe 1: click a submit button, the button is still
        # there, same URL. The expected text "click the submit button"
        # must NOT verify — the word "submit" is a verb, not an id.
        task_id = loop.create_task(self.conn, "Log in to the test portal")
        loop.plan_task(self.conn, task_id, call_fn=_mock_plan)
        loop.execute_step(self.conn, task_id, 3, LOGIN_OBS, call_fn=_mock_action_for(3))
        result = loop.verify_step(
            self.conn, task_id, 3, LOGIN_OBS,
            "click the submit button", previous_url=LOGIN_OBS["url"],
            previous_observation=LOGIN_OBS,
        )
        self.assertFalse(result["verified"], result)

    def test_verify_does_not_pass_on_preexisting_confirmation_text(self):
        # Review probe 2: the page already said "Applied on 12 Nov"
        # before the click. A confirmation that was already there
        # must NOT verify the step.
        task_id = loop.create_task(self.conn, "Log in to the test portal")
        loop.plan_task(self.conn, task_id, call_fn=_mock_plan)
        loop.execute_step(self.conn, task_id, 3, LOGIN_OBS, call_fn=_mock_action_for(3))
        already_applied = {
            "url": LOGIN_OBS["url"],
            "elements": [
                {"id": "el_status", "tag": "p", "role": "generic",
                 "name": "", "text": "Applied on 12 Nov",
                 "visible": True, "interactable": False},
            ],
        }
        result = loop.verify_step(
            self.conn, task_id, 3, already_applied,
            "application submitted", previous_url=LOGIN_OBS["url"],
            previous_observation=already_applied,
        )
        self.assertFalse(result["verified"], result)

    def test_verify_does_not_pass_on_prefilled_field_value(self):
        # A type action does not verify when the field already held
        # a value before typing (the value did not change).
        task_id = loop.create_task(self.conn, "Log in to the test portal")
        loop.plan_task(self.conn, task_id, call_fn=_mock_plan)
        prefilled = self._obs_with_value("el_login_email", "old@example.com")
        loop.execute_step(self.conn, task_id, 1, prefilled, call_fn=_mock_action_for(1))
        result = loop.verify_step(
            self.conn, task_id, 1, prefilled,
            "email field holds a value", previous_url=LOGIN_OBS["url"],
            previous_observation=prefilled,
        )
        self.assertFalse(result["verified"], result)

    def test_done_step_cannot_be_reexecuted(self):
        # Never retry a submit: a step that is already done refuses
        # to run again, server-side (the durable backstop).
        task_id = loop.create_task(self.conn, "Log in to the test portal")
        loop.plan_task(self.conn, task_id, call_fn=_mock_plan)
        loop.execute_step(self.conn, task_id, 1, LOGIN_OBS, call_fn=_mock_action_for(1))
        loop.verify_step(
            self.conn, task_id, 1,
            self._obs_with_value("el_login_email", "[EMAIL_1]"),
            "email field holds a value", previous_url=LOGIN_OBS["url"],
            previous_observation=LOGIN_OBS,
        )
        with self.assertRaises(ValueError):
            loop.execute_step(self.conn, task_id, 1, LOGIN_OBS, call_fn=_mock_action_for(1))

    def test_server_pii_scan_blocks_raw_email_in_the_goal(self):
        # Review B1: the goal goes into the planner prompt, so a raw
        # email in it must be re-redacted before the outbound call.
        from server.planner import scan

        clean, hits = scan.redact(
            "Apply to the role and email aarav.test@example.com the result"
        )
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["type"], "email")
        self.assertNotIn("aarav.test@example.com", clean)
        self.assertIn("[EMAIL_1]", clean)

    def test_server_pii_scan_blocks_twice_then_stops_the_task(self):
        # Two scan blocks on the same step stop the task (§6.4).
        task_id = loop.create_task(
            self.conn, "Email aarav.test@example.com about the role"
        )
        loop.plan_task(self.conn, task_id, call_fn=_mock_plan)
        # First block: re-redacted, the call proceeds.
        loop.execute_step(self.conn, task_id, 1, LOGIN_OBS, call_fn=_mock_action_for(1))
        # Second block on the same step: the task stops.
        with self.assertRaises(validate.ValidationError):
            loop.execute_step(self.conn, task_id, 1, LOGIN_OBS, call_fn=_mock_action_for(1))
        self.assertEqual(loop.get_task(self.conn, task_id)["status"], "failed")

    def test_local_only_constraint_reaches_the_planner(self):
        # Review M2: a task whose constraints say "local only" must
        # call the planner with local_only=True (cloud is never
        # silently used).
        seen: dict = {}

        def capturing_planner(prompt, local_only=False, **kwargs):
            seen["local_only"] = local_only
            return _mock_plan(prompt)

        task_id = loop.create_task(
            self.conn, "Log in", "local only"
        )
        loop.plan_task(self.conn, task_id, call_fn=capturing_planner)
        self.assertTrue(seen.get("local_only"), seen)

    def test_checkpoint_summary_is_10_lines_max(self):
        task_id = loop.create_task(self.conn, "Do a thing")
        loop.write_checkpoint(
            self.conn, task_id, 1, "\n".join(f"line {i}" for i in range(20)), {"step": 1}
        )
        summary = loop.checkpoint_summary(self.conn, task_id)
        self.assertEqual(len(summary.splitlines()), 10)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _request(base_url, method, path, token=TOKEN, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{base_url}{path}", data=data, method=method)
    if token is not None:
        req.add_header("X-Eclipse-Token", token)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())
    except urllib.error.URLError:
        return 0, {}


class TestTasksApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.port = _free_port()
        cls.base = f"http://127.0.0.1:{cls.port}"
        env = dict(os.environ)
        env["ECLIPSE_DATA_DIR"] = cls.tmp.name
        env["ECLIPSE_TOKEN"] = TOKEN
        # No planner keys, and Ollama pointed at a dead port: the
        # planner must fail closed (502), never hang or crash.
        env.pop("ANTSEED_API_KEY", None)
        env.pop("GROQ_API_KEY", None)
        env["OLLAMA_BASE_URL"] = "http://127.0.0.1:1/v1"
        venv_python = REPO_ROOT / ".venv" / "bin" / "python"
        python = str(venv_python) if venv_python.exists() else sys.executable
        cls.proc = subprocess.Popen(
            [python, "-m", "uvicorn", "server.main:app",
             "--host", "127.0.0.1", "--port", str(cls.port)],
            cwd=str(REPO_ROOT), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + 20
        while time.time() < deadline:
            status, _ = _request(cls.base, "GET", "/health")
            if status == 200:
                break
            time.sleep(0.3)
        else:
            raise RuntimeError("server did not come up for tasks API test")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(timeout=10)
        cls.tmp.cleanup()

    def test_create_and_get_task(self):
        status, body = _request(self.base, "POST", "/tasks",
                                body={"goal": "Log in to the test portal"})
        self.assertEqual(status, 201, body)
        task_id = body["id"]
        status, body = _request(self.base, "GET", f"/tasks/{task_id}")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["goal"], "Log in to the test portal")
        self.assertEqual(body["steps"], [])
        self.assertEqual(body["checkpoints"], [])

    def test_get_task_flattens_step_description(self):
        # Sprint 9 fix (review B3): the steps table has no
        # description column — it lives inside action_json. The
        # GET /tasks/{id} response must carry a real description
        # per step, or the workspace renders "Step N: " blank.
        import json as _json
        import sqlite3 as _sqlite3

        status, body = _request(self.base, "POST", "/tasks",
                                body={"goal": "Apply to Acme Tech"})
        self.assertEqual(status, 201, body)
        task_id = body["id"]
        conn = _sqlite3.connect(f"{self.tmp.name}/eclipse.db")
        conn.execute(
            "INSERT INTO steps (task_id, n, action_json, status, result, retries)"
            " VALUES (?, 1, ?, 'done', 'ok', 0)",
            (task_id, _json.dumps({"action": "click", "element_id": "el_apply_1",
                                   "description": "Click the Acme Tech apply button"})),
        )
        conn.commit()
        conn.close()
        status, body = _request(self.base, "GET", f"/tasks/{task_id}")
        self.assertEqual(status, 200, body)
        self.assertEqual(len(body["steps"]), 1)
        self.assertEqual(body["steps"][0]["description"],
                         "Click the Acme Tech apply button")

    def test_tasks_require_token(self):
        status, _ = _request(self.base, "POST", "/tasks",
                             body={"goal": "x"}, token=None)
        self.assertEqual(status, 401)

    def test_missing_task_is_404(self):
        status, _ = _request(self.base, "GET", "/tasks/nope")
        self.assertEqual(status, 404)

    def test_plan_fails_closed_without_a_planner(self):
        status, body = _request(self.base, "POST", "/tasks",
                                body={"goal": "Log in"})
        task_id = body["id"]
        status, body = _request(self.base, "POST", f"/tasks/{task_id}/plan")
        self.assertEqual(status, 502, body)

    def test_cancel_and_list(self):
        status, body = _request(self.base, "POST", "/tasks",
                                body={"goal": "Do a thing"})
        task_id = body["id"]
        status, body = _request(self.base, "POST", f"/tasks/{task_id}/cancel")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["status"], "cancelled")
        status, body = _request(self.base, "GET", "/tasks?status=cancelled")
        self.assertEqual(status, 200, body)
        self.assertTrue(any(t["id"] == task_id for t in body["tasks"]))

    def test_approve_needs_an_approval_id(self):
        # Review M6: approve records the approval (audit + grant),
        # so the endpoint takes an approval_id in the body.
        status, body = _request(self.base, "POST", "/tasks",
                                body={"goal": "Do a thing"})
        task_id = body["id"]
        status, body = _request(self.base, "POST", f"/tasks/{task_id}/approve",
                                body={})
        self.assertEqual(status, 422, body)
        status, body = _request(self.base, "POST", f"/tasks/{task_id}/approve",
                                body={"approval_id": "appr_123"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["approval_id"], "appr_123")

    def test_verify_needs_the_baseline_observation(self):
        # Review B2/N6: verify is a baseline diff, so the API
        # requires previous_observation (and previous_url).
        status, body = _request(self.base, "POST", "/tasks",
                                body={"goal": "Do a thing"})
        task_id = body["id"]
        obs = {"url": "http://localhost/login.html", "elements": []}
        status, body = _request(
            self.base, "POST", f"/tasks/{task_id}/steps/1/verify",
            body={"observation": obs, "expected_end_state": "done",
                  "previous_url": "http://localhost/login.html"},
        )
        self.assertEqual(status, 422, body)


if __name__ == "__main__":
    unittest.main()
