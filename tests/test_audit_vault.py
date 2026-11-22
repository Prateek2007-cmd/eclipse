"""Audit regression tests (vision + vault area): the vault file,
per-task grants, the fill map and the vault API.

Run: .venv/bin/python -m unittest tests.test_audit_vault -v
All card numbers / passwords here are synthetic test data.
"""
from __future__ import annotations

import stat
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

from fastapi import HTTPException

from server.api import vault_api
from server.core import config, db
from server.jobs import profile
from server.vault import fill as fill_mod
from server.vault import grants as grant_store
from server.vault.store import Vault, VaultError, is_never_stored_field

PASSWORD = "correct horse battery staple"
CARD = {"number": "4111 1111 1111 1111", "expiry": "12/28", "cvv": "123"}


class TestIdleLockOnTheDataPath(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Vault(data_dir=Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def _idle_11_minutes(self):
        self.vault._last_used_at = int(time.time()) - 11 * 60

    def test_get_password_after_idle_is_refused_without_checking_unlocked(self):
        # The idle lock used to live only in the `unlocked`
        # property: any caller that read without checking it first
        # pulled passwords out of an idle-timed-out vault.
        self.vault.create(PASSWORD)
        self.vault.store_password("https://example.com", "password", "s3cr3t-value")
        self._idle_11_minutes()
        # NOTE: deliberately NOT touching .unlocked first.
        with self.assertRaises(VaultError):
            self.vault.get_password("https://example.com", "password")
        self.assertFalse(self.vault.unlocked)

    def test_store_and_list_after_idle_are_refused(self):
        self.vault.create(PASSWORD)
        self._idle_11_minutes()
        with self.assertRaises(VaultError):
            self.vault.store_password("https://example.com", "password", "x")
        with self.assertRaises(VaultError):
            self.vault.list_origins()

    def test_use_refreshes_the_idle_clock(self):
        self.vault.create(PASSWORD)
        self.vault.store_password("https://example.com", "password", "s3cr3t")
        self._idle_11_minutes()
        # A fresh unlock resets the clock; a read right after works.
        self.vault.unlock(PASSWORD)
        self.assertEqual(
            self.vault.get_password("https://example.com", "password"), "s3cr3t"
        )


class TestVaultFileRobustness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Vault(data_dir=Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_malformed_header_is_a_vault_error_not_a_key_error(self):
        # Valid JSON with missing keys used to raise a bare
        # KeyError (an unhandled 500 in the API).
        path = Path(self.tmp.name) / "vault.fernet"
        path.write_text('{"foo": 1}')
        with self.assertRaises(VaultError):
            self.vault.unlock(PASSWORD)
        path.write_text('{"salt": 123, "entries": 456}')
        with self.assertRaises(VaultError):
            self.vault.unlock(PASSWORD)

    def test_vault_file_is_0600_even_when_chmod_never_runs(self):
        # The temp file used to be created with the process umask
        # (0660 here) and chmodded afterwards — a window where the
        # file was group-readable. It is now created 0600.
        with mock.patch("os.chmod", lambda *a, **k: None):
            self.vault.create(PASSWORD)
        mode = stat.S_IMODE((Path(self.tmp.name) / "vault.fernet").stat().st_mode)
        self.assertEqual(mode, 0o600)


class TestNeverStoredFields(unittest.TestCase):
    def test_matcher_refuses_variants_of_card_cvv_otp(self):
        for field in ("card_number", "card", "cardnumber", "cc_number",
                      "ccnum", "credit_card", "cvv", "mycvv", "cvc",
                      "security_cvc", "otp", "otp_code"):
            self.assertTrue(is_never_stored_field(field), field)
        for field in ("password", "username", "email", "api_key", "token"):
            self.assertFalse(is_never_stored_field(field), field)

    def test_store_refuses_a_card_number_by_any_field_name(self):
        # "cardnumber" used to slip past the exact-match list and
        # the card number landed in the vault file.
        with tempfile.TemporaryDirectory() as tmp:
            vault = Vault(data_dir=Path(tmp))
            vault.create(PASSWORD)
            for field in ("cardnumber", "cc_number", "mycvv"):
                with self.assertRaises(VaultError, msg=field):
                    vault.store_password(
                        "https://shop.example.com", field,
                        "4111 1111 1111 1111",
                    )
            self.assertIsNone(
                vault.get_password("https://shop.example.com", "cardnumber")
            )
            # A real password still stores.
            vault.store_password("https://example.com", "password", "s3cr3t")
            self.assertEqual(
                vault.get_password("https://example.com", "password"), "s3cr3t"
            )


class TestGrantExpirySweeper(unittest.TestCase):
    def test_purge_expired_wipes_the_card(self):
        store = grant_store.GrantStore()
        grant = store.create("task-1", card=dict(CARD))
        grant.expires_at = int(time.time()) - 1  # expired
        self.assertEqual(store.purge_expired(), 1)
        self.assertIsNone(grant.card, "the card must be wiped on expiry")
        self.assertIsNone(store.get("task-1"))

    def test_purge_expired_leaves_live_grants_alone(self):
        store = grant_store.GrantStore()
        live = store.create("task-live", card=dict(CARD))
        dead = store.create("task-dead", card=dict(CARD))
        dead.expires_at = int(time.time()) - 1
        self.assertEqual(store.purge_expired(), 1)
        self.assertIsNotNone(store.get("task-live"))
        self.assertIsNotNone(live.card)

    def test_sweeper_wipes_an_expired_grant_without_any_read(self):
        # An expired grant's card used to linger in memory until
        # some read happened to touch the store. The sweeper
        # wipes it in the background.
        store = grant_store.GrantStore(sweep_interval_s=0.05)
        grant = store.create("task-1", card=dict(CARD))
        grant.expires_at = int(time.time()) - 1
        deadline = time.time() + 2.0
        while grant.card is not None and time.time() < deadline:
            time.sleep(0.05)
        self.assertIsNone(grant.card, "sweeper did not wipe the expired card")
        self.assertIsNone(store.get("task-1"))


class TestTokenBinding(unittest.TestCase):
    def test_label_match_is_on_whole_words(self):
        # The old substring match filled [PHONE_1] into "Hotel
        # preferences" ("tel") and [NAME_1] into "Username".
        self.assertFalse(fill_mod._token_belongs_to_element(
            "[PHONE_1]", {"name": "Hotel preferences", "text": "", "value": ""}))
        self.assertFalse(fill_mod._token_belongs_to_element(
            "[NAME_1]", {"name": "Username", "text": "", "value": ""}))
        self.assertFalse(fill_mod._token_belongs_to_element(
            "[ROLL_1]", {"name": "Enrollment", "text": "", "value": ""}))
        # Real labels still bind.
        self.assertTrue(fill_mod._token_belongs_to_element(
            "[NAME_1]", {"name": "Full name", "text": "", "value": ""}))
        self.assertTrue(fill_mod._token_belongs_to_element(
            "[PHONE_1]", {"name": "Phone number", "text": "", "value": ""}))
        self.assertTrue(fill_mod._token_belongs_to_element(
            "[PHONE_1]", {"name": "Mobile", "text": "", "value": ""}))
        self.assertTrue(fill_mod._token_belongs_to_element(
            "[CARD_LAST4]", {"name": "Card number", "text": "", "value": ""}))
        self.assertTrue(fill_mod._token_belongs_to_element(
            "[CVV]", {"name": "Security code", "text": "", "value": ""}))
        # The token in the redacted value still binds on its own.
        self.assertTrue(fill_mod._token_belongs_to_element(
            "[PHONE_1]", {"name": "Something else", "text": "", "value": "[PHONE_1]"}))
        # No element → no fill (fail-closed).
        self.assertFalse(fill_mod._token_belongs_to_element("[PHONE_1]", None))


class TestGrantOriginScoping(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old = config.DATA_DIR
        config.DATA_DIR = Path(self.tmp.name)
        db.migrate()
        self.conn = db.connect()
        self.vault = Vault(data_dir=Path(self.tmp.name))
        profile.store_profile(self.conn, profile.TEST_PROFILE)
        self.addCleanup(grant_store.GRANTS.revoke_all)

    def tearDown(self):
        self.conn.close()
        config.DATA_DIR = self._old
        self.tmp.cleanup()

    def _approval_grant(self, task_id):
        now = int(time.time())
        self.conn.execute(
            "INSERT INTO grants (id, origin, scope, expires_at, created_at)"
            " VALUES (?, ?, 'approve', ?, ?)",
            (uuid.uuid4().hex[:12], task_id, now + 600, now),
        )
        self.conn.commit()
        return [{"id": "g1"}]

    def test_scoped_grant_fills_only_on_its_origin(self):
        self.vault.create(PASSWORD)
        grant_store.GRANTS.create(
            "task-1", card=dict(CARD), origin="https://shop.example.com"
        )
        approvals = self._approval_grant("task-1")
        target = {"id": "el_card_number", "name": "Card number",
                  "text": "", "value": "[CARD_NUMBER]"}
        # The grant's own origin fills.
        fill, denied = fill_mod.build_fill_map(
            self.conn, "task-1", ["[CARD_NUMBER]"],
            "https://shop.example.com", self.vault,
            approval_grants=approvals, target=target,
        )
        self.assertIsNone(denied)
        self.assertEqual(fill["[CARD_NUMBER]"], "4111 1111 1111 1111")
        # Another origin does not.
        fill, denied = fill_mod.build_fill_map(
            self.conn, "task-1", ["[CARD_NUMBER]"],
            "https://other.com", self.vault,
            approval_grants=approvals, target=target,
        )
        self.assertEqual(fill, {})
        self.assertIn("shop.example.com", denied or "")
        # An empty origin cannot prove it is the grant's origin.
        fill, denied = fill_mod.build_fill_map(
            self.conn, "task-1", ["[CARD_NUMBER]"],
            "", self.vault, approval_grants=approvals, target=target,
        )
        self.assertEqual(fill, {})
        self.assertIsNotNone(denied)

    def test_unscoped_grant_still_fills_anywhere(self):
        self.vault.create(PASSWORD)
        grant_store.GRANTS.create("task-1", card=dict(CARD))
        approvals = self._approval_grant("task-1")
        fill, denied = fill_mod.build_fill_map(
            self.conn, "task-1", ["[CARD_NUMBER]"],
            "https://anywhere.example.com", self.vault,
            approval_grants=approvals,
            target={"id": "el_card_number", "name": "Card number",
                    "text": "", "value": "[CARD_NUMBER]"},
        )
        self.assertIsNone(denied)
        self.assertEqual(fill["[CARD_NUMBER]"], "4111 1111 1111 1111")


class TestUnlockOriginWiring(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Vault(data_dir=Path(self.tmp.name))
        self.vault.create(PASSWORD)
        self.store = grant_store.GrantStore()

    def tearDown(self):
        self.tmp.cleanup()

    def _unlock(self, **kwargs):
        body = vault_api.UnlockIn(password=PASSWORD, task_id="task-1", **kwargs)
        conn = mock.MagicMock()
        with (
            mock.patch.object(vault_api.db, "connect", return_value=conn),
            mock.patch.object(
                vault_api.loop, "get_task", return_value={"id": "task-1"}
            ),
            mock.patch.object(vault_api, "VAULT", self.vault),
            mock.patch.object(grant_store, "GRANTS", self.store),
        ):
            return vault_api.unlock(body)

    def test_unlock_scopes_the_grant_to_the_given_origin(self):
        # The unlock endpoint used to never pass an origin, so
        # every grant was unscoped and filled on any site.
        body = self._unlock(
            card_number="4111 1111 1111 1111",
            card_expiry="12/28",
            card_cvv="123",
            origin="https://shop.example.com:443/checkout",
        )
        self.assertEqual(body["origin"], "https://shop.example.com")
        grant = self.store.get("task-1")
        self.assertIsNotNone(grant)
        self.assertEqual(grant.origin, "https://shop.example.com")

    def test_unlock_without_an_origin_leaves_the_grant_unscoped(self):
        body = self._unlock()
        self.assertIsNone(body["origin"])
        self.assertIsNone(self.store.get("task-1").origin)

    def test_unlock_rejects_an_unparseable_origin(self):
        with self.assertRaises(HTTPException) as ctx:
            self._unlock(origin="not a url")
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIsNone(self.store.get("task-1"))

    def test_normalise_origin(self):
        self.assertEqual(
            vault_api._normalise_origin("https://Shop.Example.COM:443/a?b=c"),
            "https://shop.example.com",
        )
        self.assertEqual(
            vault_api._normalise_origin("http://localhost:8000/"),
            "http://localhost",
        )
        self.assertEqual(vault_api._normalise_origin("not a url"), "")
        self.assertEqual(vault_api._normalise_origin("ftp://example.com"), "")
        self.assertEqual(vault_api._normalise_origin(""), "")


if __name__ == "__main__":
    unittest.main()
