"""Mission models: CRUD + state machine + activity log (PR #5, §3.2–3.4).

Conventions: ids are "m_<12 hex>" / "mr_<12 hex>" / "ma_<12 hex>";
timestamps are integer SECONDS (never ms — the audit log is the
only ms table in the system). Config/bounds are validated against
the type registry (server/missions/types.py, W2) and stored as
JSON; getters return them parsed.

Errors follow the loop.py convention: KeyError = unknown id (the
API maps it to 404), IllegalTransitionError (a ValueError) = state
refusal (409), plain ValueError = validation failure (422).
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid

from ..core import audit

MISSION_STATUSES = ("draft", "active", "paused", "stopped", "done")
TERMINAL_MISSION_STATUSES = ("stopped", "done")

# The state machine (§3.4). "done" is reached from "active" only
# (the runner, when a one-shot completion condition is met).
LEGAL_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "draft": ("active", "stopped"),
    "active": ("paused", "stopped", "done"),
    "paused": ("active", "stopped"),
    "stopped": (),
    "done": (),
}

# Closed activity-kind enum (§3.2).
ACTIVITY_KINDS = (
    "run_started", "discovered", "matched", "drafted",
    "approval_needed", "approved", "applied", "sent", "refused",
    "failed", "waiting_for_extension", "briefing_included",
    "state_changed", "price_observed",
)

# Terminal task states — keep in sync with
# server/api/tasks_api.py::TERMINAL_TASK_STATUSES (importing the
# API module from the model layer would invert the dependency).
TERMINAL_TASK_STATUSES = ("cancelled", "failed", "done")

MAX_NAME_LEN = 120


class MissionNotFoundError(KeyError):
    """Unknown mission id (404)."""


class IllegalTransitionError(ValueError):
    """A state refusal: the transition or edit is not allowed from
    the mission's current status (409). A ValueError subclass so
    `except ValueError` still catches it; the API catches this
    class first to tell 409 apart from 422 validation errors."""


def _now() -> int:
    return int(time.time())


def _row_to_mission(row: sqlite3.Row) -> dict:
    mission = dict(row)
    for key in ("config", "bounds"):
        raw = mission.pop(f"{key}_json", None)
        try:
            mission[key] = json.loads(raw) if raw else {}
        except (ValueError, TypeError):
            mission[key] = {}
    return mission


def _validate_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise ValueError("name must not be empty")
    if len(name) > MAX_NAME_LEN:
        raise ValueError(f"name must be at most {MAX_NAME_LEN} characters")
    return name


def create_mission(conn, type: str, name: str, config: dict,
                   bounds: dict) -> dict:
    """Create a mission in status "draft". Config/bounds are
    validated against the type schema (unknown keys rejected);
    bounds default per type when omitted. Raises ValueError on
    an unknown type or a schema failure."""
    # Imported here, not at module level: types.py is W2's file
    # and may not exist yet while the workers build in parallel.
    from . import types as _types

    name = _validate_name(name)
    config = _types.validate_config(type, config or {})
    bounds = _types.validate_bounds(type, bounds or {})
    mission_id = f"m_{uuid.uuid4().hex[:12]}"
    now = _now()
    conn.execute(
        "INSERT INTO missions (id, type, name, status, config_json,"
        " bounds_json, created_at, updated_at, last_run_at,"
        " next_run_at, last_run_status, last_error)"
        " VALUES (?, ?, ?, 'draft', ?, ?, ?, ?, NULL, NULL, NULL, NULL)",
        (mission_id, type, name, json.dumps(config),
         json.dumps(bounds), now, now),
    )
    conn.commit()
    return get_mission(conn, mission_id)


def get_mission(conn, mission_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM missions WHERE id = ?", (mission_id,)
    ).fetchone()
    return _row_to_mission(row) if row is not None else None


def list_missions(conn, status: str | None = None) -> list[dict]:
    if status is not None:
        rows = conn.execute(
            "SELECT * FROM missions WHERE status = ?"
            " ORDER BY created_at DESC, rowid DESC",
            (status,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM missions ORDER BY created_at DESC, rowid DESC"
        ).fetchall()
    return [_row_to_mission(r) for r in rows]


def transition(conn, mission_id: str, to_status: str) -> dict:
    """Move a mission to a new status. Raises KeyError for an
    unknown mission, ValueError for an unknown target status, and
    IllegalTransitionError for a transition the state machine
    forbids. Records a state_changed activity row. Activating
    sets next_run_at = now (an immediate first tick); pausing or
    stopping clears it."""
    mission = get_mission(conn, mission_id)
    if mission is None:
        raise MissionNotFoundError(f"no mission {mission_id}")
    if to_status not in MISSION_STATUSES:
        raise ValueError(f"unknown mission status {to_status!r}")
    from_status = mission["status"]
    if to_status not in LEGAL_TRANSITIONS[from_status]:
        raise IllegalTransitionError(
            f"mission {mission_id} is {from_status};"
            f" cannot move to {to_status}"
        )
    now = _now()
    next_run_at = now if to_status == "active" else None
    conn.execute(
        "UPDATE missions SET status = ?, updated_at = ?,"
        " next_run_at = ? WHERE id = ?",
        (to_status, now, next_run_at, mission_id),
    )
    conn.commit()
    record_activity(conn, mission_id, "state_changed",
                    {"from": from_status, "to": to_status})
    return get_mission(conn, mission_id)


def update_mission(conn, mission_id: str, name=None, config=None,
                   bounds=None) -> dict:
    """Rename / reconfigure a mission. Allowed in draft and
    paused; forbidden while active (pause first — a running
    mission's config must not change mid-run) and in terminal
    states. Raises KeyError / IllegalTransitionError / ValueError
    (schema failure)."""
    # Imported here, not at module level: types.py is W2's file
    # (see create_mission).
    from . import types as _types

    mission = get_mission(conn, mission_id)
    if mission is None:
        raise MissionNotFoundError(f"no mission {mission_id}")
    status = mission["status"]
    if status == "active":
        raise IllegalTransitionError(
            f"mission {mission_id} is active; pause it before editing"
        )
    if status in TERMINAL_MISSION_STATUSES:
        raise IllegalTransitionError(
            f"mission {mission_id} is {status}; it cannot be edited"
        )
    updates: dict = {"updated_at": _now()}
    if name is not None:
        updates["name"] = _validate_name(name)
    if config is not None:
        updates["config_json"] = json.dumps(
            _types.validate_config(mission["type"], config))
    if bounds is not None:
        updates["bounds_json"] = json.dumps(
            _types.validate_bounds(mission["type"], bounds))
    if len(updates) == 1:
        return mission
    assignments = ", ".join(f"{k} = ?" for k in updates)
    conn.execute(
        f"UPDATE missions SET {assignments} WHERE id = ?",
        (*updates.values(), mission_id),
    )
    conn.commit()
    return get_mission(conn, mission_id)


def stop_mission(conn, mission_id: str) -> dict:
    """The kill switch (§3.5 rule 4): terminal and immediate.

    Side effects, in order: revoke the mission's live approval
    grants, consume its pending approvals, release its role
    claims (loop.release_claims_for_task per task), cancel its
    non-terminal spawned tasks — then transition to "stopped"
    and write the audit entry. The side effects run before the
    transition so a failed stop can be retried (the deletes and
    updates are idempotent; a mission already stopped 409s
    before any side effect runs).
    """
    from ..tasks import loop

    mission = get_mission(conn, mission_id)
    if mission is None:
        raise MissionNotFoundError(f"no mission {mission_id}")
    if mission["status"] in TERMINAL_MISSION_STATUSES:
        raise IllegalTransitionError(
            f"mission {mission_id} is {mission['status']};"
            " it is already terminal"
        )
    task_ids = [
        r["id"] for r in conn.execute(
            "SELECT id FROM tasks WHERE mission_id = ?", (mission_id,)
        ).fetchall()
    ]
    for task_id in task_ids:
        conn.execute(
            "DELETE FROM grants WHERE origin = ? AND scope = 'approve'",
            (task_id,),
        )
        conn.execute(
            "DELETE FROM pending_approvals WHERE task_id = ?", (task_id,)
        )
        loop.release_claims_for_task(conn, task_id)
    conn.execute(
        "UPDATE tasks SET status = 'cancelled', updated_at = ?"
        " WHERE mission_id = ? AND status NOT IN"
        " ('cancelled', 'failed', 'done')",
        (_now(), mission_id),
    )
    conn.commit()
    stopped = transition(conn, mission_id, "stopped")
    audit.append(conn, None, "mission.stopped",
                 {"mission_id": mission_id})
    return stopped


def delete_mission(conn, mission_id: str) -> None:
    """Delete a draft or stopped mission, with its runs and
    activity. Receipts, drafts and tasks are kept — they are the
    audit trail. Raises KeyError for an unknown mission and
    IllegalTransitionError while active/paused."""
    mission = get_mission(conn, mission_id)
    if mission is None:
        raise MissionNotFoundError(f"no mission {mission_id}")
    if mission["status"] in ("active", "paused"):
        raise IllegalTransitionError(
            f"mission {mission_id} is {mission['status']};"
            " stop it before deleting"
        )
    conn.execute(
        "DELETE FROM mission_activity WHERE mission_id = ?", (mission_id,))
    conn.execute(
        "DELETE FROM mission_runs WHERE mission_id = ?", (mission_id,))
    conn.execute("DELETE FROM missions WHERE id = ?", (mission_id,))
    conn.commit()


def record_activity(conn, mission_id: str, kind: str, detail: dict,
                    run_id: str | None = None) -> str:
    """Append one activity row (the mission's receipt log).
    Returns the activity id. `detail` is redacted JSON — ids,
    counts, reasons, never raw PII. Raises KeyError for an
    unknown mission and ValueError for an unknown kind."""
    if kind not in ACTIVITY_KINDS:
        raise ValueError(
            f"unknown activity kind {kind!r} — use one of"
            f" {', '.join(ACTIVITY_KINDS)}"
        )
    if get_mission(conn, mission_id) is None:
        raise MissionNotFoundError(f"no mission {mission_id}")
    activity_id = f"ma_{uuid.uuid4().hex[:12]}"
    conn.execute(
        "INSERT INTO mission_activity (id, mission_id, run_id, kind,"
        " detail, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (activity_id, mission_id, run_id, kind,
         json.dumps(detail or {}), _now()),
    )
    conn.commit()
    return activity_id


def list_activity(conn, mission_id: str, limit: int = 50,
                  kind: str | None = None) -> list[dict]:
    """Newest first. `detail` is returned as the stored JSON
    string (the §3.3 API shape)."""
    if kind is not None:
        rows = conn.execute(
            "SELECT * FROM mission_activity"
            " WHERE mission_id = ? AND kind = ?"
            " ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (mission_id, kind, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM mission_activity WHERE mission_id = ?"
            " ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (mission_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]
