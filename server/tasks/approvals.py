"""The shared approve helper (PR #5): consume a pending approval
and mint the scoped grant.

Extracted verbatim from server/api/tasks_api.py::approve_step
(PR #4, the Bug 3 fix) so the mission runner's auto-satisfy path
(Q1 standing approval) uses the exact same gate as the human
approve endpoint — one gate, two callers, no drift.

Errors follow the loop.py convention: KeyError = unknown id (the
API maps it to 404), ValueError = state refusal (the API maps it
to 409).
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid

from ..core import audit
from . import loop

GRANT_TTL_S = 600  # 10 minutes, same as the pending-approval window

# Terminal task states — keep in sync with
# server/api/tasks_api.py::TERMINAL_TASK_STATUSES (importing the
# API module from here would be circular: tasks_api imports this
# module).
TERMINAL_TASK_STATUSES = ("cancelled", "failed", "done")


def is_replayed(conn, task_id: str, approval_id: str) -> bool:
    """Has this approval_id already minted a grant for this task?
    The approval_id rides in the grant detail (metadata, not
    scope) so a retried POST — a double-click, a network retry —
    is one approval, not two. Checked before the pending lookup,
    so a replay of a CONSUMED approval still 409s (its pending
    record is gone) instead of 404ing."""
    for row in conn.execute(
        "SELECT detail FROM grants WHERE origin = ? AND scope = 'approve'",
        (task_id,),
    ).fetchall():
        try:
            existing = json.loads(row["detail"] or "{}")
        except (ValueError, TypeError):
            existing = {}
        if existing.get("approval_id") == approval_id:
            return True
    return False


def approve_pending(conn, task_id: str, approval_id: str,
                    expect_draft_id: str | None = None) -> dict:
    """Approve a pending step (the approval card flow, §5.4/§6.1).

    The approval_id must name a LIVE pending approval recorded
    by the task loop when the step reached the human gate (Bug 3:
    the old handler accepted any made-up id, on any task state,
    because no pending-approval store existed at all). An unknown
    id is a KeyError; a pending approval on a finished task, past
    its expiry, or replayed is a ValueError. On success the
    pending approval is consumed and a 10-minute grant is minted
    — one approval = one action. The grant's scope comes from the
    pending record, not the caller: nobody can widen what the
    gate asked for.

    `expect_draft_id` (mission approve-mail only): the pending
    record must name this draft in its detail, or a submit
    approval could be spent on a mail send. A mismatch is a
    ValueError, raised before anything is consumed.
    """
    now = int(time.time())
    # Idempotency (§5.4: one approval, one grant) — see
    # is_replayed. Checked before the pending lookup, so a
    # replay of a CONSUMED approval still 409s.
    if is_replayed(conn, task_id, approval_id):
        raise ValueError(
            f"approval {approval_id} already recorded for this task"
        )
    # The pending approval is the gate: unknown (or recorded for
    # a different task) is a 404 — no pending record, no grant,
    # whatever the task state.
    pending = loop.get_pending_approval(conn, approval_id)
    if pending is None or pending["task_id"] != task_id:
        raise KeyError(
            f"no pending approval {approval_id} for this task"
        )
    # A finished task cannot be approved back to life.
    task = loop.get_task(conn, task_id)
    if task is None:
        raise KeyError(f"no task {task_id}")
    if task["status"] in TERMINAL_TASK_STATUSES:
        raise ValueError(
            f"task {task_id} is {task['status']}; refusing to approve"
        )
    # An expired approval is a 409, and the stale record is
    # dropped so it cannot be approved later.
    if int(pending["expires_at"] or 0) <= now:
        loop.consume_pending_approval(conn, approval_id)
        raise ValueError(f"approval {approval_id} has expired")
    try:
        scope = json.loads(pending["detail"] or "{}")
    except (ValueError, TypeError):
        scope = {}
    if (expect_draft_id is not None
            and scope.get("draft_id") != expect_draft_id):
        raise ValueError(
            f"approval {approval_id} does not cover draft"
            f" {expect_draft_id}"
        )
    # Consume the pending approval FIRST, so a concurrent second
    # POST finds nothing to consume (the grant replay check above
    # is the second layer, not the only one).
    loop.consume_pending_approval(conn, approval_id)
    audit.append(
        conn, task_id, "task.approve",
        {"approval_id": approval_id},
    )
    detail = {
        "approval_id": approval_id,
        "step": pending["step"],
        **{
            k: v for k, v in {
                "element_id": scope.get("element_id"),
                "role_id": scope.get("role_id"),
                # PR #5: a mail-send approval names the draft it
                # covers, so the existing send endpoint finds the
                # grant (mail_api._live_grant_for_draft). Task-loop
                # pending records never carry draft_id, so task
                # approvals are unaffected.
                "draft_id": scope.get("draft_id"),
            }.items() if v is not None
        },
    }
    conn.execute(
        "INSERT INTO grants (id, origin, scope, expires_at, created_at, detail)"
        " VALUES (?, ?, 'approve', ?, ?, ?)",
        (uuid.uuid4().hex[:12], task_id, now + GRANT_TTL_S, now,
         json.dumps(detail)),
    )
    conn.execute(
        "UPDATE tasks SET status = 'planned', updated_at = ?"
        " WHERE id = ? AND status = 'ask_user'",
        (now, task_id),
    )
    conn.commit()
    task = loop.get_task(conn, task_id)
    status = task["status"] if task else "planned"
    return {"task_id": task_id, "status": status,
            "approval_id": approval_id}
