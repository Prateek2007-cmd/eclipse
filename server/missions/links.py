"""Mission links: mission <-> task / approval joins (PR #5, §4).

One mission = one task per run; the link is the `mission_id`
column on `tasks`, set by the runner right after `create_task`.
Pending approvals are NOT duplicated per mission — they live in
the existing `pending_approvals` table against the run's task,
and mission-level listing is a join (live approvals only).
"""
from __future__ import annotations

import json
import sqlite3
import time


def tasks_for_mission(conn, mission_id: str) -> list[dict]:
    """The mission's spawned tasks (newest first)."""
    rows = conn.execute(
        "SELECT id, goal, status, created_at FROM tasks"
        " WHERE mission_id = ? ORDER BY created_at DESC, rowid DESC",
        (mission_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _approval_row(row: sqlite3.Row) -> dict:
    """Flatten one pending_approvals row the way
    tasks_api.list_pending_approvals does: the detail JSON's
    scope keys (element_id, role_id, draft_id) are lifted to
    top-level fields."""
    try:
        detail = json.loads(row["detail"] or "{}")
    except (ValueError, TypeError):
        detail = {}
    return {
        "approval_id": row["id"], "task_id": row["task_id"],
        "step": row["step"], "action": row["action"],
        **{k: v for k, v in detail.items() if v is not None},
        "created_at": row["created_at"], "expires_at": row["expires_at"],
    }


def pending_approvals_for_mission(conn, mission_id: str) -> list[dict]:
    """Live pending approvals across the mission's spawned tasks
    (oldest first — the one waiting longest is the one to act
    on). Expired approvals are not listed."""
    now = int(time.time())
    rows = conn.execute(
        "SELECT p.id, p.task_id, p.step, p.action, p.detail,"
        " p.created_at, p.expires_at"
        " FROM pending_approvals p JOIN tasks t ON p.task_id = t.id"
        " WHERE t.mission_id = ? AND p.expires_at > ?"
        " ORDER BY p.created_at ASC",
        (mission_id, now),
    ).fetchall()
    return [_approval_row(r) for r in rows]


def all_pending_approvals(conn) -> list[dict]:
    """Live pending approvals across every mission (the aggregate
    the extension alarm calls once per cycle, §3.9, instead of
    one call per mission). Each row carries its mission_id."""
    now = int(time.time())
    rows = conn.execute(
        "SELECT p.id, p.task_id, p.step, p.action, p.detail,"
        " p.created_at, p.expires_at, t.mission_id AS mission_id"
        " FROM pending_approvals p JOIN tasks t ON p.task_id = t.id"
        " WHERE t.mission_id IS NOT NULL AND p.expires_at > ?"
        " ORDER BY p.created_at ASC",
        (now,),
    ).fetchall()
    out = []
    for row in rows:
        approval = _approval_row(row)
        approval["mission_id"] = row["mission_id"]
        out.append(approval)
    return out


def set_task_mission(conn, task_id: str, mission_id: str) -> None:
    """Link a spawned task to its mission (the runner calls this
    right after loop.create_task — no change to loop.py)."""
    conn.execute(
        "UPDATE tasks SET mission_id = ? WHERE id = ?",
        (mission_id, task_id),
    )
    conn.commit()
