"""Per-task vault grants (Sprint 5, plan §6.7).

A grant is created by POST /vault/unlock (password + task_id).
It authorises the vault to fill values for that task. It expires
per task or after 15 minutes, whichever is first. The vault locks
on 10 minutes of idle (no fill/unlock call). Revoke-all is one
click.

A grant may carry per-task card details (entered per task, never
stored): the card number, expiry and CVV live only in the grant
(in memory) and are wiped when the grant expires or is revoked.
The card's last 4 digits are the only part ever shown (the
approval card shows them).

Grants live in memory (a dict, guarded by a lock) — never in
SQLite, never in a log. A server restart drops them, which is
the safe default (the vault locks).
"""
from __future__ import annotations

import threading
import time
import uuid

GRANT_TTL_S = 15 * 60  # 15 minutes, per the plan
IDLE_LOCK_S = 10 * 60  # lock on 10 minutes of idle


class VaultGrant:
    def __init__(self, task_id: str, card: dict | None = None,
                 origin: str | None = None):
        self.id = uuid.uuid4().hex[:12]
        self.task_id = task_id
        self.created_at = int(time.time())
        self.expires_at = self.created_at + GRANT_TTL_S
        self.last_used_at = self.created_at
        # The origin the card may be filled on (the checkout
        # site). A grant without an origin fills on any allowed
        # site; a grant with one fills only there.
        self.origin = origin
        # Per-task card details (never stored): number, expiry, cvv.
        # Wiped on expiry/revoke. Only the last 4 are ever shown.
        self.card = dict(card) if card else None

    @property
    def card_last4(self) -> str | None:
        if not self.card:
            return None
        number = str(self.card.get("number") or "").replace(" ", "")
        return number[-4:] if len(number) >= 4 else None

    def expired(self, now: int | None = None) -> bool:
        now = int(time.time()) if now is None else now
        return now >= self.expires_at or now - self.last_used_at >= IDLE_LOCK_S

    def touch(self) -> None:
        self.last_used_at = int(time.time())

    def wipe(self) -> None:
        """Drop the per-task card details from memory."""
        self.card = None


class GrantStore:
    """The in-memory grant table.

    A daemon sweeper thread purges expired grants (wiping their
    card details) within one sweep interval of expiry. Without
    it, an expired grant's card lingered in memory until some
    read happened to touch the store — "wiped on expiry" was
    only true on read, and a grant nobody re-read was never
    wiped at all. The sweeper starts lazily on the first grant
    (never at import), so a store that is never used spawns no
    thread.
    """

    def __init__(self, sweep_interval_s: float = 60):
        self._grants: dict[str, VaultGrant] = {}
        self._lock = threading.Lock()
        self._sweep_interval_s = sweep_interval_s
        self._sweeper_started = False

    def _start_sweeper(self) -> None:
        if self._sweeper_started:
            return
        self._sweeper_started = True

        def _sweep_forever() -> None:
            while True:
                time.sleep(self._sweep_interval_s)
                self.purge_expired()

        threading.Thread(
            target=_sweep_forever, daemon=True, name="vault-grant-sweeper"
        ).start()

    def purge_expired(self) -> int:
        """Wipe and drop every expired grant (TTL or idle).
        Returns the count purged."""
        now = int(time.time())
        with self._lock:
            doomed = [
                grant_id
                for grant_id, grant in self._grants.items()
                if grant.expired(now)
            ]
            for grant_id in doomed:
                self._grants[grant_id].wipe()
                del self._grants[grant_id]
            return len(doomed)

    def create(self, task_id: str, card: dict | None = None,
               origin: str | None = None) -> VaultGrant:
        grant = VaultGrant(task_id, card, origin)
        with self._lock:
            self._grants[grant.id] = grant
            self._start_sweeper()
        return grant

    def get(self, task_id: str) -> VaultGrant | None:
        """The valid grant for a task, or None. Expired grants are
        wiped and dropped on read."""
        with self._lock:
            for grant_id, grant in list(self._grants.items()):
                if grant.task_id != task_id:
                    continue
                if grant.expired():
                    grant.wipe()
                    del self._grants[grant_id]
                    continue
                return grant
        return None

    def revoke_task(self, task_id: str) -> int:
        """Revoke every grant for a task. Returns the count."""
        with self._lock:
            doomed = [g for g in self._grants.values() if g.task_id == task_id]
            for grant in doomed:
                grant.wipe()
                del self._grants[grant.id]
            return len(doomed)

    def revoke_all(self) -> int:
        """Revoke every grant (one click). Returns the count."""
        with self._lock:
            count = len(self._grants)
            for grant in self._grants.values():
                grant.wipe()
            self._grants.clear()
            return count

    def status(self, task_id: str | None = None) -> dict:
        """The vault status: the grants (ids, tasks, expiry — never
        the card values) and whether any grant is live. Expired
        grants are wiped and dropped on read (their card data
        must not linger in memory after expiry)."""
        now = int(time.time())
        with self._lock:
            live = []
            for grant_id, grant in list(self._grants.items()):
                if grant.expired(now):
                    grant.wipe()
                    del self._grants[grant_id]
                    continue
                if task_id is not None and grant.task_id != task_id:
                    continue
                live.append({
                    "grant_id": grant.id,
                    "task_id": grant.task_id,
                    "expires_at": grant.expires_at,
                    "expires_in_s": max(0, grant.expires_at - now),
                    "card_last4": grant.card_last4,
                    "has_card": grant.card is not None,
                })
        return {"has_grants": bool(live), "grants": live}


# The process-wide grant store (in memory; a restart drops it).
GRANTS = GrantStore()
