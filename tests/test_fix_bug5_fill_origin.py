"""Fix regression tests — Bug 5 (MEDIUM): POST /vault/fill ignores
origin scoping.

Root cause: the probe never checked the grant's origin scope, while
the value-release path (server/vault/fill.py) enforces it. With a
grant scoped to https://shop.example.com, a probe from
https://evil.example.com returned 200 {ok:true, has_card:true,
card_last4:"1111"}. The probe never returns the value, but it must
not confirm a card exists on an origin the grant does not cover.

The fix enforces the same fail-closed rule as fill.py: a scoped
grant answers only on its own origin; an empty/unparseable origin
cannot prove it is the grant's origin, so it is denied too. An
unscoped grant still answers anywhere.

All card numbers / passwords here are synthetic test data.
"""
import json
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from server.api import vault_api  # noqa: E402
from server.core import config, db  # noqa: E402
from server.tasks import loop  # noqa: E402
from server.vault import grants as grant_store  # noqa: E402
from server.vault.store import Vault  # noqa: E402

PASSWORD = "correct-horse-battery-" + "staple"
CARD = {"number": "4111 1111 1111 1111", "expiry": "12/28", "cvv": "123"}
SHOP = "https://shop.example.com"
EVIL = "https://evil.example.com"

CARD_OBS = {
    "url": SHOP + "/checkout",
    "elements": [
        {"id": "el_card", "tag": "input", "role": "textbox",
         "name": "Card number", "text": "", "value": "[CARD_NUMBER]",
         "visible": True, "interactable": True},
    ],
}


def _evil_obs():
    obs = json.loads(json.dumps(CARD_OBS))
    obs["url"] = EVIL + "/checkout"
    return obs


def _mock_plan(prompt, **kw):
    return (json.dumps([
        {"step": 1, "description": "type the card number",
         "expected_end_state": "card field holds a value"},
    ]), "mock:planner", 5.0)


def _mock_type_card(prompt, **kw):
    return (json.dumps({"action": "type", "element_id": "el_card",
                        "text": "[CARD_NUMBER]",
                        "reason": "fill the card"}), "mock:planner", 5.0)


class Bug5Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig_data_dir = config.DATA_DIR
        config.DATA_DIR = Path(self.tmp.name)
        db.migrate()
        self.conn = db.connect()
        vault_dir = Path(self.tmp.name) / "vault"
        vault_dir.mkdir()
        self.vault = Vault(data_dir=vault_dir)
        self.vault.create(PASSWORD)
        self.store = grant_store.GrantStore()
        self._vault_patch = mock.patch.object(vault_api, "VAULT", self.vault)
        self._grants_patch = mock.patch.object(
            grant_store, "GRANTS", self.store)
        self._vault_patch.start()
        self._grants_patch.start()

    def tearDown(self):
        self._grants_patch.stop()
        self._vault_patch.stop()
        self.conn.close()
        config.DATA_DIR = self._orig_data_dir
        self.tmp.cleanup()

    def _probe(self, task_id, origin, field="card_number"):
        return vault_api.fill(vault_api.FillIn(
            task_id=task_id, origin=origin, field=field))

    def _approval_grant(self, task_id):
        now = int(time.time())
        self.conn.execute(
            "INSERT INTO grants (id, origin, scope, expires_at, created_at)"
            " VALUES (?, ?, 'approve', ?, ?)",
            (uuid.uuid4().hex[:12], task_id, now + 600, now),
        )
        self.conn.commit()


class TestBug5ProbeOrigin(Bug5Base):
    def test_matching_origin_probe_succeeds(self):
        # Guard: the grant's own origin still probes fine.
        self.store.create("task-1", card=dict(CARD), origin=SHOP)
        probe = self._probe("task-1", SHOP)
        self.assertTrue(probe["ok"], probe)
        self.assertTrue(probe["has_card"])
        self.assertEqual(probe["card_last4"], "1111")

    def test_mismatched_origin_probe_is_denied(self):
        # The live bug: an evil-origin probe returned
        # {ok:true, has_card:true, card_last4:"1111"}.
        self.store.create("task-1", card=dict(CARD), origin=SHOP)
        probe = self._probe("task-1", EVIL)
        self.assertFalse(probe["ok"], probe)
        self.assertNotIn("has_card", probe)
        self.assertNotIn("card_last4", probe)

    def test_empty_origin_probe_is_denied(self):
        # An origin that cannot be parsed (normalises to "") cannot
        # prove it is the grant's origin — denied, like fill.py.
        self.store.create("task-1", card=dict(CARD), origin=SHOP)
        probe = self._probe("task-1", "not a url at all")
        self.assertFalse(probe["ok"], probe)

    def test_unscoped_grant_probe_still_answers_anywhere(self):
        # Guard: a grant with no origin scope is not restricted —
        # the fix must not break unscoped grants.
        self.store.create("task-1", card=dict(CARD))
        probe = self._probe("task-1", EVIL)
        self.assertTrue(probe["ok"], probe)


class TestBug5ValueReleaseEnforcesOrigin(Bug5Base):
    """Live verification of the value-release path through the loop:
    execute_step builds the fill map (the only path that releases
    the real card number) — a scoped grant must fill only on its
    own origin, end to end, not just in build_fill_map unit tests.
    """

    def _checkout_task(self):
        task_id = loop.create_task(self.conn, "Pay for the test order")
        loop.plan_task(self.conn, task_id, call_fn=_mock_plan)
        self.store.create(task_id, card=dict(CARD), origin=SHOP)
        self._approval_grant(task_id)
        return task_id

    def test_execute_fills_on_the_grant_origin(self):
        task_id = self._checkout_task()
        result = loop.execute_step(self.conn, task_id, 1, CARD_OBS,
                                   call_fn=_mock_type_card)
        self.assertEqual(result["fill"].get("[CARD_NUMBER]"),
                         "4111 1111 1111 1111", result)

    def test_execute_refuses_to_fill_on_another_origin(self):
        task_id = self._checkout_task()
        with self.assertRaises(ValueError) as ctx:
            loop.execute_step(self.conn, task_id, 1, _evil_obs(),
                              call_fn=_mock_type_card)
        self.assertIn("shop.example.com", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
