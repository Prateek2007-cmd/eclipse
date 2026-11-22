"""Receipts (Sprint 4, plan Flow 1 step 5, §6.9).

After a submit is VERIFIED (not merely executed), a receipt is
recorded: confirmation page text, time, link, and the role. The
task's report lists exactly which roles succeeded and which did
not, with receipts for the successes. A receipt is the durable
record that a role was applied to — the already-applied check
reads it before every submit, so a role is never re-submitted.

Table `receipts` is created by db.migrate() (Sprint 4).
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid


def record_receipt(
    conn,
    task_id: str,
    role_id: str,
    title: str,
    company: str,
    url: str,
    confirmation: str,
    status: str = "submitted",
) -> str:
    """Record a receipt for a verified submit. Returns the id. The
    confirmation is redacted before it is stored — a confirmation
    containing the applicant's name or email must not land in
    SQLite in the clear.

    Idempotent per ROLE, not just per (task, role): a role is
    applied to once, ever, so a SUBMITTED receipt on file for
    the role — from any task — is returned instead of recording
    a second one. The check and the insert run under BEGIN
    IMMEDIATE (when the caller holds no transaction), so two
    tasks racing to record the same role serialize: the loser
    sees the winner's receipt instead of writing its own.

    A FAILED receipt does not block the retry: when the retry
    succeeds, the failed row is upgraded to submitted (a stale
    "failed" row must not outlive the submit it describes, and
    returning the failed row's id as if it were the success
    would lie to the caller). A submitted receipt is never
    downgraded to failed."""
    from ..planner import scan

    clean_confirmation, _hits = scan.redact(confirmation or "")
    began = False
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
        began = True
    try:
        if status == "submitted":
            existing = receipt_for_role(conn, role_id)
            if existing is not None:
                conn.commit()
                return existing["id"]
        receipt_id = uuid.uuid4().hex[:12]
        try:
            conn.execute(
                "INSERT INTO receipts (id, task_id, role_id, title, company, url,"
                " status, confirmation, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    receipt_id, task_id, role_id, title, company, url,
                    status, clean_confirmation, int(time.time()),
                ),
            )
        except sqlite3.IntegrityError:
            # A receipt already exists for (task, role).
            row = conn.execute(
                "SELECT * FROM receipts WHERE task_id = ? AND role_id = ?",
                (task_id, role_id),
            ).fetchone()
            if row is None:
                raise
            if row["status"] == status:
                conn.commit()
                return row["id"]
            if row["status"] == "failed" and status == "submitted":
                conn.execute(
                    "UPDATE receipts SET title = ?, company = ?, url = ?,"
                    " status = 'submitted', confirmation = ?, created_at = ?"
                    " WHERE id = ?",
                    (title, company, url, clean_confirmation,
                     int(time.time()), row["id"]),
                )
                conn.commit()
                return row["id"]
            # A submitted receipt is never downgraded to failed.
            conn.commit()
            return row["id"]
        conn.commit()
        return receipt_id
    except Exception:
        if began:
            conn.rollback()
        raise


def list_receipts(conn, task_id: str) -> list[dict]:
    """All receipts for a task, oldest first."""
    rows = conn.execute(
        "SELECT * FROM receipts WHERE task_id = ? ORDER BY created_at ASC",
        (task_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def receipt_for_role(conn, role_id: str) -> dict | None:
    """The latest SUBMITTED receipt for a role (any task), or None
    when never applied. A failed receipt does not count — a role
    whose submit failed (a site timeout) must be retryable, not
    permanently blocked."""
    row = conn.execute(
        "SELECT * FROM receipts WHERE role_id = ? AND status = 'submitted'"
        " ORDER BY created_at DESC LIMIT 1",
        (role_id,),
    ).fetchone()
    return dict(row) if row else None


def task_report(conn, task_id: str) -> dict:
    """The §6.9 compensation report: exactly which roles succeeded
    and which did not, with receipts for the successes."""
    receipts = list_receipts(conn, task_id)
    return {
        "task_id": task_id,
        "submitted": [r for r in receipts if r["status"] == "submitted"],
        "failed": [r for r in receipts if r["status"] == "failed"],
        "count": len(receipts),
    }
