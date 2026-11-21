"""Hash-chained audit log. Append-only.

hash = SHA256(prev_hash | action | payload_hash | time)

Honest note: a local hash chain proves tampering after the fact, it
does not prevent it. It also cannot detect deletion of the tail or
truncation of the whole table — a shorter chain still verifies. Say so
in the docs; do not claim tamper-proof. A later sprint anchors the
latest hash externally (e.g. the tip hash on each export) so
truncation becomes detectable.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time

GENESIS = "0" * 64

_lock = threading.Lock()


def _payload_hash(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()


def _chain_hash(prev_hash: str, action: str, payload_hash: str,
                created_at: int) -> str:
    return hashlib.sha256(
        f"{prev_hash}|{action}|{payload_hash}|{created_at}".encode()
    ).hexdigest()


def append(conn: sqlite3.Connection, task_id: str | None, action: str,
           payload: object) -> dict:
    """Append one entry and return its id and hash. Commits."""
    conn.row_factory = sqlite3.Row
    with _lock:
        row = conn.execute(
            "SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
        prev = row["hash"] if row else GENESIS
        created_at = int(time.time() * 1000)
        payload_hash = _payload_hash(payload)
        entry_hash = _chain_hash(prev, action, payload_hash, created_at)
        cur = conn.execute(
            "INSERT INTO audit_log"
            "(task_id, action, payload_hash, prev_hash, hash, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (task_id, action, payload_hash, prev, entry_hash, created_at),
        )
        conn.commit()
        return {"id": cur.lastrowid, "hash": entry_hash,
                "created_at": created_at}


def verify(conn: sqlite3.Connection) -> dict:
    """Recompute the whole hash chain. Returns ok / checked / first bad.

    `checked` is the number of entries that verified before the first
    bad one (all of them on success).
    """
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, action, payload_hash, prev_hash, hash, created_at"
        " FROM audit_log ORDER BY id"
    ).fetchall()
    prev = GENESIS
    for index, row in enumerate(rows):
        if row["prev_hash"] != prev:
            return {"ok": False, "checked": index,
                    "first_bad_id": row["id"],
                    "reason": "prev_hash does not match previous entry"}
        expected = _chain_hash(prev, row["action"], row["payload_hash"],
                               row["created_at"])
        if expected != row["hash"]:
            return {"ok": False, "checked": index,
                    "first_bad_id": row["id"],
                    "reason": "hash does not recompute"}
        prev = row["hash"]
    return {"ok": True, "checked": len(rows), "first_bad_id": None,
            "reason": None}
