"""Already-applied check (Sprint 4, plan §6.9).

Before every submit, check whether the role was already applied
to — in the receipts table (the durable record) and in the memory
table (a key "applied: <role_id>" is also recorded on success).
A role that already succeeded is never re-submitted, even if the
planner asks for it again. Only SUBMITTED receipts count — a
failed submit must be retryable.
"""
from __future__ import annotations

from . import receipts


def is_already_applied(conn, role_id: str, url: str | None = None) -> bool:
    """True when a SUBMITTED receipt exists for this role (by role
    id or URL), or a memory entry records the application."""
    if receipts.receipt_for_role(conn, role_id):
        return True
    if url:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM receipts"
            " WHERE url = ? AND status = 'submitted'",
            (url,),
        ).fetchone()
        if row["c"]:
            return True
    # Memory table: "applied: <role_id>" (key/value).
    for key in (f"applied: {role_id}", f"applied: {url}" if url else ""):
        if not key:
            continue
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM memory WHERE key = ?", (key,)
        ).fetchone()
        if row["c"]:
            return True
    return False


def mark_applied(conn, role_id: str, url: str | None = None) -> None:
    """Record the application in the memory table (in addition to
    the receipt, which the caller records separately)."""
    conn.execute(
        "INSERT OR REPLACE INTO memory (key, value, kind, updated_at)"
        " VALUES (?, ?, 'applied', strftime('%s','now'))",
        (f"applied: {role_id}", url or role_id),
    )
    conn.commit()
