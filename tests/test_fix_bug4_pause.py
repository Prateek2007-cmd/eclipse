"""Fix regression tests — Bug 4 (MEDIUM): pause resurrects
cancelled tasks.

Root cause: POST /tasks/{id}/pause (and /cancel) used an unguarded
_set_status, unlike resume_task which correctly 409s on terminal
states. Pausing a cancelled task flipped it to "paused", and resume
then worked — a cancelled task came back to life.

All test data is fake (Test User, example.com).
"""
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from fastapi import HTTPException  # noqa: E402
from server.api import tasks_api  # noqa: E402
from server.core import config, db  # noqa: E402
from server.tasks import loop  # noqa: E402

TERMINAL = ("cancelled", "failed", "done")


class Bug4Base(unittest.TestCase):
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

    def _task_in_state(self, status):
        task_id = loop.create_task(self.conn, "Do a thing")
        self.conn.execute(
            "UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))
        self.conn.commit()
        return task_id


class TestBug4PauseOnTerminal(Bug4Base):
    def test_pause_on_cancelled_is_409(self):
        # The live bug: pause on a cancelled task returned 200
        # {"status": "paused"} — and resume then worked.
        task_id = self._task_in_state("cancelled")
        with self.assertRaises(HTTPException) as ctx:
            tasks_api.pause_task(task_id)
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(loop.get_task(self.conn, task_id)["status"],
                         "cancelled")

    def test_pause_on_failed_and_done_is_409(self):
        for status in ("failed", "done"):
            with self.subTest(status=status):
                task_id = self._task_in_state(status)
                with self.assertRaises(HTTPException) as ctx:
                    tasks_api.pause_task(task_id)
                self.assertEqual(ctx.exception.status_code, 409)
                self.assertEqual(
                    loop.get_task(self.conn, task_id)["status"], status)

    def test_cancel_on_terminal_is_409(self):
        # Cancel has the same hole: cancelling a cancelled/failed/
        # done task must not silently "succeed" — terminal states
        # are final, the same rule resume_task already enforces.
        for status in TERMINAL:
            with self.subTest(status=status):
                task_id = self._task_in_state(status)
                with self.assertRaises(HTTPException) as ctx:
                    tasks_api.cancel_task(task_id)
                self.assertEqual(ctx.exception.status_code, 409)
                self.assertEqual(
                    loop.get_task(self.conn, task_id)["status"], status)


class TestBug4LiveTasksStillWork(Bug4Base):
    def test_pause_and_resume_on_live_task(self):
        # Guard: pause/resume on a live task still work exactly
        # as before.
        task_id = loop.create_task(self.conn, "Do a thing")
        result = tasks_api.pause_task(task_id)
        self.assertEqual(result["status"], "paused")
        result = tasks_api.resume_task(task_id)
        self.assertEqual(result["status"], "planned")

    def test_cancel_on_live_task(self):
        # Guard: cancelling a live task still works.
        task_id = loop.create_task(self.conn, "Do a thing")
        result = tasks_api.cancel_task(task_id)
        self.assertEqual(result["status"], "cancelled")

    def test_pause_twice_is_idempotent(self):
        # Pausing an already-paused task is a no-op success, not
        # an error — "paused" is not a terminal state.
        task_id = loop.create_task(self.conn, "Do a thing")
        tasks_api.pause_task(task_id)
        result = tasks_api.pause_task(task_id)
        self.assertEqual(result["status"], "paused")


if __name__ == "__main__":
    unittest.main()
