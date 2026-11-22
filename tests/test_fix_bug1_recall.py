"""Fix regression tests — Bug 1 (HIGH): planning crashes with a 502
whenever a stored memory fact matches the goal.

Root cause: server/memory/keyword.py::recall() returns list[dict],
but server/tasks/loop.py::_memory_facts() passed it through typed as
list[str], and server/planner/prompts.py::build_plan_prompt does
"\\n".join(memory_facts) -> TypeError -> 502. The same list is also
joined in loop.execute_step's prompt assembly, so the execute path
is pinned here too.

Mock planners only — no API key, no live model, no network.
All test data is fake (Test User, example.com).
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from server.core import config, db  # noqa: E402
from server.memory import keyword  # noqa: E402
from server.tasks import loop  # noqa: E402

SEARCH_OBS = {
    "url": "https://example.com/jobs",
    "elements": [
        {"id": "el_search", "tag": "button", "role": "button",
         "name": "Search", "text": "Search",
         "visible": True, "interactable": True},
    ],
}


def _mock_plan(prompt, **kw):
    return (json.dumps([
        {"step": 1, "description": "do step 1",
         "expected_end_state": "step 1 done"},
    ]), "mock:planner", 5.0)


def _mock_click(element_id):
    def fn(prompt, **kw):
        return (json.dumps({"action": "click", "element_id": element_id,
                            "reason": "click it"}), "mock:planner", 5.0)
    return fn


class Bug1Base(unittest.TestCase):
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


class TestBug1PlanWithMatchingFact(Bug1Base):
    def test_memory_facts_returns_strings(self):
        # _memory_facts is typed list[str] — recall() returns
        # dicts, so the extraction must happen here.
        keyword.store_fact(self.conn, "prefers remote SDE roles")
        facts = loop._memory_facts(self.conn, "Apply to remote SDE roles")
        self.assertEqual(len(facts), 1)
        self.assertIsInstance(facts[0], str)
        self.assertEqual(facts[0], "prefers remote SDE roles")

    def test_plan_succeeds_with_matching_fact(self):
        # The live crash: plan_task raised TypeError (a 502 at the
        # API) as soon as any stored fact matched the goal.
        keyword.store_fact(self.conn, "prefers remote SDE roles")
        task_id = loop.create_task(self.conn, "Apply to remote SDE roles")
        steps = loop.plan_task(self.conn, task_id, call_fn=_mock_plan)
        self.assertEqual(len(steps), 1)
        self.assertEqual(loop.get_task(self.conn, task_id)["status"],
                         "planned")

    def test_fact_text_reaches_the_plan_prompt(self):
        # The fix must not just avoid the crash — the fact's text
        # still has to reach the planner (dropping facts silently
        # would "fix" the crash by breaking memory).
        keyword.store_fact(self.conn, "prefers remote SDE roles")
        seen: list[str] = []

        def capturing(prompt, **kw):
            seen.append(prompt)
            return _mock_plan(prompt, **kw)

        task_id = loop.create_task(self.conn, "Apply to remote SDE roles")
        loop.plan_task(self.conn, task_id, call_fn=capturing)
        self.assertEqual(len(seen), 1)
        self.assertIn("prefers remote SDE roles", seen[0])

    def test_execute_succeeds_with_matching_fact(self):
        # execute_step joins the same _memory_facts list into its
        # prompt ("REMEMBERED FACTS:\n" + "\\n".join(facts)) — the
        # same TypeError crashed execution too.
        keyword.store_fact(self.conn, "prefers remote SDE roles")
        task_id = loop.create_task(self.conn, "Find remote SDE roles")
        loop.plan_task(self.conn, task_id, call_fn=_mock_plan)
        result = loop.execute_step(self.conn, task_id, 1, SEARCH_OBS,
                                   call_fn=_mock_click("el_search"))
        self.assertEqual(result["action"]["element_id"], "el_search")

    def test_no_matching_fact_still_plans(self):
        # Guard: with no matching fact, planning works exactly as
        # before (no facts section in the prompt).
        keyword.store_fact(self.conn, "likes strong coffee")
        seen: list[str] = []

        def capturing(prompt, **kw):
            seen.append(prompt)
            return _mock_plan(prompt, **kw)

        task_id = loop.create_task(self.conn, "Apply to remote SDE roles")
        loop.plan_task(self.conn, task_id, call_fn=capturing)
        self.assertNotIn("REMEMBERED FACTS", seen[0])


if __name__ == "__main__":
    unittest.main()
