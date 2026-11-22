"""Fix regression tests — Bug 3 (HIGH): POST /tasks/{id}/approve
minted a real grant for any made-up approval_id, on any task state.

Root cause: the handler checked only replay idempotency — there was
NO pending-approval store at all, so the human gate was enforced by
UI flow only, not by the server. The fix adds a pending_approvals
table: the task loop records a pending approval when a step reaches
the human gate, and approve requires a live pending record for that
task (404 unknown, 409 terminal/expired, consumed on success so a
replay still 409s).

Mock planners only — no API key, no live model, no network.
All test data is fake (Test User, example.com, localhost fixtures).
"""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from fastapi import HTTPException  # noqa: E402
from server.api import tasks_api  # noqa: E402
from server.core import config, db  # noqa: E402
from server.tasks import loop  # noqa: E402

FORM_OBS = {
    "url": "http://localhost/application-form.html",
    "elements": [
        {"id": "el_app_submit", "tag": "button", "role": "button",
         "name": "Submit application", "text": "", "value": "",
         "visible": True, "interactable": True},
    ],
}

CARD_OBS = {
    "url": "https://shop.example.com/checkout",
    "elements": [
        {"id": "el_card", "tag": "input", "role": "textbox",
         "name": "Card number", "text": "", "value": "[CARD_NUMBER]",
         "visible": True, "interactable": True},
    ],
}

ROLE_CONSTRAINTS = "role: role_1\nurl: http://localhost/jobs/role_1"


def _mock_plan(n_steps=1):
    def fn(prompt, **kw):
        return (json.dumps([
            {"step": i, "description": f"do step {i}",
             "expected_end_state": f"step {i} done"}
            for i in range(1, n_steps + 1)
        ]), "mock:planner", 5.0)
    return fn


def _mock_click(element_id):
    def fn(prompt, **kw):
        return (json.dumps({"action": "click", "element_id": element_id,
                            "reason": "click it"}), "mock:planner", 5.0)
    return fn


def _mock_type_card(prompt, **kw):
    return (json.dumps({"action": "type", "element_id": "el_card",
                        "text": "[CARD_NUMBER]",
                        "reason": "fill the card"}), "mock:planner", 5.0)


class Bug3Base(unittest.TestCase):
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

    def _grants_left(self, task_id):
        return self.conn.execute(
            "SELECT COUNT(*) AS c FROM grants WHERE origin = ?"
            " AND scope = 'approve'",
            (task_id,),
        ).fetchone()["c"]

    def _pending_left(self, task_id):
        return self.conn.execute(
            "SELECT COUNT(*) AS c FROM pending_approvals WHERE task_id = ?",
            (task_id,),
        ).fetchone()["c"]


class TestBug3UnknownApproval(Bug3Base):
    def test_made_up_id_is_404_and_mints_no_grant(self):
        # The live bug: any made-up id on a created task returned
        # 200 with a real 10-minute grant.
        task_id = loop.create_task(self.conn, "Do a thing")
        with self.assertRaises(HTTPException) as ctx:
            tasks_api.approve_step(
                task_id, tasks_api.ApproveIn(approval_id="made-up-123"))
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(self._grants_left(task_id), 0)

    def test_made_up_id_on_cancelled_task_is_404(self):
        task_id = loop.create_task(self.conn, "Do a thing")
        tasks_api.cancel_task(task_id)
        with self.assertRaises(HTTPException) as ctx:
            tasks_api.approve_step(
                task_id, tasks_api.ApproveIn(approval_id="made-up-123"))
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(self._grants_left(task_id), 0)

    def test_approval_for_another_task_is_404(self):
        # A pending approval is scoped to its own task — its id
        # must not mint a grant on a different task.
        task_a = loop.create_task(self.conn, "Do a thing")
        task_b = loop.create_task(self.conn, "Do another thing")
        approval_id = loop.record_pending_approval(
            self.conn, task_a, step=1, action="submit")
        with self.assertRaises(HTTPException) as ctx:
            tasks_api.approve_step(
                task_b, tasks_api.ApproveIn(approval_id=approval_id))
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(self._grants_left(task_b), 0)
        # And the real task can still approve with it.
        result = tasks_api.approve_step(
            task_a, tasks_api.ApproveIn(approval_id=approval_id))
        self.assertEqual(result["status"], "created")


class TestBug3TerminalAndExpired(Bug3Base):
    def test_cancelled_task_with_pending_approval_is_409(self):
        # The live bug: a made-up id on a CANCELLED task returned
        # 200 + a real grant. A real pending approval on a task
        # that has since been cancelled must not mint one either.
        task_id = loop.create_task(self.conn, "Do a thing")
        approval_id = loop.record_pending_approval(
            self.conn, task_id, step=1, action="submit")
        tasks_api.cancel_task(task_id)
        with self.assertRaises(HTTPException) as ctx:
            tasks_api.approve_step(
                task_id, tasks_api.ApproveIn(approval_id=approval_id))
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(self._grants_left(task_id), 0)
        self.assertEqual(loop.get_task(self.conn, task_id)["status"],
                         "cancelled")

    def test_expired_approval_is_409_and_mints_no_grant(self):
        task_id = loop.create_task(self.conn, "Do a thing")
        approval_id = loop.record_pending_approval(
            self.conn, task_id, step=1, action="submit")
        # Force the approval past its expiry.
        self.conn.execute(
            "UPDATE pending_approvals SET expires_at = ? WHERE id = ?",
            (int(time.time()) - 1, approval_id),
        )
        self.conn.commit()
        with self.assertRaises(HTTPException) as ctx:
            tasks_api.approve_step(
                task_id, tasks_api.ApproveIn(approval_id=approval_id))
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(self._grants_left(task_id), 0)


class TestBug3ValidApproval(Bug3Base):
    def test_valid_pending_approval_mints_one_grant(self):
        task_id = loop.create_task(self.conn, "Do a thing")
        approval_id = loop.record_pending_approval(
            self.conn, task_id, step=1, action="submit")
        result = tasks_api.approve_step(
            task_id, tasks_api.ApproveIn(approval_id=approval_id))
        self.assertEqual(result["approval_id"], approval_id)
        self.assertEqual(self._grants_left(task_id), 1)
        # Consumed: the pending record is gone.
        self.assertEqual(self._pending_left(task_id), 0)

    def test_replay_is_409_and_mints_no_second_grant(self):
        # One approval, one grant: a retried POST (a double-click,
        # a network retry) is one approval, not two submits.
        task_id = loop.create_task(self.conn, "Do a thing")
        approval_id = loop.record_pending_approval(
            self.conn, task_id, step=1, action="submit")
        tasks_api.approve_step(
            task_id, tasks_api.ApproveIn(approval_id=approval_id))
        with self.assertRaises(HTTPException) as ctx:
            tasks_api.approve_step(
                task_id, tasks_api.ApproveIn(approval_id=approval_id))
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(self._grants_left(task_id), 1)

    def test_two_distinct_pending_approvals_mint_two_grants(self):
        # Guard: two separate pending approvals (two cards) still
        # mint two grants — the fix must not collapse distinct
        # approvals into one.
        task_id = loop.create_task(self.conn, "Do a thing")
        first = loop.record_pending_approval(
            self.conn, task_id, step=1, action="submit")
        second = loop.record_pending_approval(
            self.conn, task_id, step=2, action="submit")
        self.assertNotEqual(first, second)
        tasks_api.approve_step(
            task_id, tasks_api.ApproveIn(approval_id=first))
        tasks_api.approve_step(
            task_id, tasks_api.ApproveIn(approval_id=second))
        self.assertEqual(self._grants_left(task_id), 2)

    def test_pending_approvals_endpoint_lists_live_ones(self):
        # The UI needs a way to learn the server-issued id: the
        # list shows live pending approvals for the task only.
        task_id = loop.create_task(self.conn, "Do a thing")
        live_id = loop.record_pending_approval(
            self.conn, task_id, step=1, action="submit")
        stale_id = loop.record_pending_approval(
            self.conn, task_id, step=2, action="submit")
        self.conn.execute(
            "UPDATE pending_approvals SET expires_at = ? WHERE id = ?",
            (int(time.time()) - 1, stale_id),
        )
        self.conn.commit()
        result = tasks_api.list_pending_approvals(task_id)
        ids = [row["approval_id"] for row in result["approvals"]]
        self.assertEqual(ids, [live_id])


class TestBug3GateRecordsApproval(Bug3Base):
    def test_consequential_gate_records_a_pending_approval(self):
        # End to end through the loop: a submit step with no grant
        # reaches the human gate, which RECORDS a pending approval
        # (the id is in the error, so the caller can approve it);
        # approving it unblocks the task and the step executes.
        task_id = loop.create_task(
            self.conn, "Apply to role_1 (Acme Tech)", ROLE_CONSTRAINTS)
        loop.plan_task(self.conn, task_id, call_fn=_mock_plan(1))
        with self.assertRaises(ValueError) as ctx:
            loop.execute_step(self.conn, task_id, 1, FORM_OBS,
                              call_fn=_mock_click("el_app_submit"))
        self.assertIn("approval", str(ctx.exception).lower())
        self.assertEqual(loop.get_task(self.conn, task_id)["status"],
                         "ask_user")
        pending = tasks_api.list_pending_approvals(task_id)["approvals"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["step"], 1)
        approval_id = pending[0]["approval_id"]
        self.assertIn(approval_id, str(ctx.exception))
        # Approve with the recorded id, then the step executes.
        result = tasks_api.approve_step(
            task_id, tasks_api.ApproveIn(approval_id=approval_id))
        self.assertEqual(result["status"], "planned")
        executed = loop.execute_step(self.conn, task_id, 1, FORM_OBS,
                                     call_fn=_mock_click("el_app_submit"))
        self.assertEqual(executed["action"]["element_id"], "el_app_submit")
        self.assertEqual(self._grants_left(task_id), 0)

    def test_repeated_gate_reuses_the_same_pending_approval(self):
        # Re-executing the gated step (the user has not approved
        # yet) must not mint a second pending approval for the
        # same gate — one gate, one approval id.
        task_id = loop.create_task(
            self.conn, "Apply to role_1 (Acme Tech)", ROLE_CONSTRAINTS)
        loop.plan_task(self.conn, task_id, call_fn=_mock_plan(1))
        for _ in range(2):
            with self.assertRaises(ValueError):
                loop.execute_step(self.conn, task_id, 1, FORM_OBS,
                                  call_fn=_mock_click("el_app_submit"))
        self.assertEqual(self._pending_left(task_id), 1)

    def test_vault_fill_gate_records_a_pending_approval(self):
        # The second gate: a secret placeholder with no fill
        # (approval BEFORE vault fill) also asks the user — and
        # must also record a pending approval, or the user could
        # never approve past it.
        task_id = loop.create_task(self.conn, "Pay for the test order")
        loop.plan_task(self.conn, task_id, call_fn=_mock_plan(1))
        with self.assertRaises(ValueError) as ctx:
            loop.execute_step(self.conn, task_id, 1, CARD_OBS,
                              call_fn=_mock_type_card)
        self.assertIn("approval", str(ctx.exception).lower())
        self.assertEqual(self._pending_left(task_id), 1)


if __name__ == "__main__":
    unittest.main()
