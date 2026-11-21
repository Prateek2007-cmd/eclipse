"""Audit endpoints: list, verify the hash chain, export."""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Query

from ..core import audit, auth, db

router = APIRouter(dependencies=[Depends(auth.require_token)], tags=["audit"])


def _rows_to_dicts(rows) -> list[dict]:
    return [dict(row) for row in rows]


@router.get("/audit")
def list_audit(task_id: str | None = Query(default=None)) -> dict:
    """List audit entries, optionally filtered by task."""
    conn = db.connect()
    try:
        if task_id:
            rows = conn.execute(
                "SELECT id, task_id, action, payload_hash, prev_hash,"
                " hash, created_at FROM audit_log"
                " WHERE task_id = ? ORDER BY id",
                (task_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, task_id, action, payload_hash, prev_hash,"
                " hash, created_at FROM audit_log ORDER BY id"
            ).fetchall()
    finally:
        conn.close()
    return {"entries": _rows_to_dicts(rows)}


@router.get("/audit/verify")
def verify_audit() -> dict:
    """Recompute the hash chain and report green or red."""
    conn = db.connect()
    try:
        return audit.verify(conn)
    finally:
        conn.close()


@router.get("/audit/export")
def export_audit() -> dict:
    """Export the full audit log as JSON (local backup, on demand)."""
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT id, task_id, action, payload_hash, prev_hash,"
            " hash, created_at FROM audit_log ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    return {
        "exported_at": int(time.time() * 1000),
        "entries": _rows_to_dicts(rows),
    }
