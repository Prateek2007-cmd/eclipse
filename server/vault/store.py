"""The encrypted vault file (Sprint 5, plan §6.7).

The vault file (`data/vault.fernet`, 0600) holds only opted-in
login passwords, one entry per (origin, field). Card numbers and
CVVs are NEVER written to it — they live only in the per-task
grant (in memory).

Format: a JSON header + a Fernet token.
  {"salt": "<base64 16 bytes>", "entries": "<Fernet token>"}
The Fernet token encrypts a JSON object:
  {"<origin>": {"<field>": "<value>", ...}, ...}
The key is PBKDF2-HMAC-SHA256 (600,000 iterations) over the
user's password + the salt. The password is never stored.

All file writes are atomic (tmp file + os.replace) and the file
is created with 0600 permissions.
"""
from __future__ import annotations

import base64
import json
import os
import stat
import threading
import time

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from ..core import config

VAULT_FILE = "vault.fernet"
PBKDF2_ITERATIONS = 600_000
SALT_BYTES = 16
# The key is dropped after 10 minutes of idle (§6.7).
IDLE_LOCK_S = 10 * 60

_lock = threading.Lock()


def vault_path(data_dir=None) -> str:
    return str((config.DATA_DIR if data_dir is None else data_dir) / VAULT_FILE)


def _derive_key(password: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=PBKDF2_ITERATIONS,
    )
    return base64.urlsafe_b64encode(kdf.derive(password.encode("utf-8")))


def _read_header(path: str) -> dict | None:
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        raise VaultError("vault file is corrupt or unreadable") from exc


def _write_file(path: str, header: dict) -> None:
    tmp = f"{path}.tmp-{os.getpid()}-{int(time.time() * 1000)}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(header, f)
    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    os.replace(tmp, path)


class VaultError(Exception):
    """Wrong password, corrupt file, or a locked vault."""


class Vault:
    """One vault file, unlocked in memory by a password.

    The Fernet key lives only in this object (in memory) for as
    long as the vault is unlocked. Locking drops the key. The
    file path is read from the current config.DATA_DIR on every
    use (tests point DATA_DIR at a temp dir after import).
    """

    def __init__(self, data_dir=None):
        self._data_dir = data_dir
        self._fernet: Fernet | None = None
        self._salt: bytes | None = None
        # The last use (unlock/store/get). The key is dropped
        # after 10 minutes of idle (§6.7: "Lock on idle 10
        # minutes") — the idle lock applies to the key, not only
        # to the grants.
        self._last_used_at = 0

    @property
    def path(self) -> str:
        return vault_path(self._data_dir)

    @property
    def unlocked(self) -> bool:
        if self._fernet is None:
            return False
        if int(time.time()) - self._last_used_at >= IDLE_LOCK_S:
            self.lock()
            return False
        return True

    def create(self, password: str) -> None:
        """Create a new vault file with this password. Fails when
        a vault already exists (unlock it instead). The first
        unlock no longer creates the vault — a mistyped first
        password must not silently become the password."""
        with _lock:
            if _read_header(self.path) is not None:
                raise VaultError("a vault already exists (unlock it instead)")
            salt = os.urandom(SALT_BYTES)
            fernet = Fernet(_derive_key(password, salt))
            token = fernet.encrypt(b"{}")
            _write_file(self.path, {
                "salt": base64.b64encode(salt).decode(),
                "entries": token.decode(),
            })
            self._salt = salt
            self._fernet = fernet
            self._last_used_at = int(time.time())

    def unlock(self, password: str) -> None:
        """Unlock with the password. Raises VaultError when no
        vault exists yet (create one first) or the password is
        wrong."""
        with _lock:
            header = _read_header(self.path)
            if header is None:
                raise VaultError("no vault yet — create one first")
            salt = base64.b64decode(header["salt"])
            fernet = Fernet(_derive_key(password, salt))
            try:
                fernet.decrypt(header["entries"].encode())
            except InvalidToken:
                raise VaultError("wrong password") from None
            self._salt = salt
            self._fernet = fernet
            self._last_used_at = int(time.time())

    def lock(self) -> None:
        """Drop the key from memory."""
        self._fernet = None
        self._salt = None
        self._last_used_at = 0

    def _entries(self) -> dict:
        if self._fernet is None:
            raise VaultError("vault is locked")
        self._last_used_at = int(time.time())
        header = _read_header(self.path)
        if header is None:
            return {}
        return json.loads(self._fernet.decrypt(header["entries"].encode()).decode())

    def _save(self, entries: dict) -> None:
        if self._fernet is None or self._salt is None:
            raise VaultError("vault is locked")
        token = self._fernet.encrypt(json.dumps(entries).encode())
        _write_file(self.path, {
            "salt": base64.b64encode(self._salt).decode(),
            "entries": token.decode(),
        })

    def store_password(self, origin: str, field: str, value: str) -> None:
        """Store an opted-in login password for (origin, field).
        Only call this when the user opted in for this site."""
        with _lock:
            entries = self._entries()
            entries.setdefault(origin, {})[field] = value
            self._save(entries)

    def get_password(self, origin: str, field: str) -> str | None:
        """Read a stored password, or None when there is none."""
        with _lock:
            return self._entries().get(origin, {}).get(field)

    def delete_password(self, origin: str, field: str) -> bool:
        """Delete a stored password. Returns True when one existed."""
        with _lock:
            entries = self._entries()
            site = entries.get(origin, {})
            if field not in site:
                return False
            del site[field]
            if not site:
                del entries[origin]
            self._save(entries)
            return True

    def list_origins(self) -> list[str]:
        """The origins with stored passwords (never the values)."""
        with _lock:
            return sorted(self._entries().keys())
