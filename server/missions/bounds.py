"""Mission bounds: cap counting + enforcement (PR #5, §3.5).

Caps are counted from SQLite (mission_activity), never from
in-memory counters — they must survive restarts. The day is the
IST calendar day (Asia/Kolkata), computed with zoneinfo, never
the host localtime. `check_bounds` is pure read + compute: it
never writes. The runner calls it immediately before spawning an
action, and writes the `refused` activity row + audit entry
itself when the check fails.
"""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

# Consequential actions and the activity kind each one settles.
ACTION_KINDS = {"apply": "applied", "send": "sent"}

# Fallbacks when a mission dict lacks a bound key (create/update
# always store full bounds, so these are a safety net, not the
# source of truth — the type defaults live in the registry).
_DEFAULT_MAX_ACTIONS = 5
_DEFAULT_MAX_APPLIES = 3
_DEFAULT_MAX_SENDS = 5


def ist_day_start(now: int | None = None) -> int:
    """Epoch seconds of 00:00 IST on the IST day containing `now`
    (default: the current time)."""
    now = int(time.time()) if now is None else int(now)
    day = datetime.fromtimestamp(now, tz=IST)
    midnight = day.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(midnight.timestamp())


def _counts_today(conn, mission_id: str, day_start: int) -> dict:
    """Applied/sent counts for the mission since `day_start`."""
    counts = {"applied": 0, "sent": 0}
    for row in conn.execute(
        "SELECT kind, COUNT(*) AS c FROM mission_activity"
        " WHERE mission_id = ? AND kind IN ('applied', 'sent')"
        " AND created_at >= ? GROUP BY kind",
        (mission_id, day_start),
    ).fetchall():
        counts[row["kind"]] = row["c"]
    return counts


def check_bounds(conn, mission: dict, action: str,
                 now: int | None = None) -> tuple[bool, str | None]:
    """Is one more `action` ("apply" | "send") within this
    mission's caps for today (IST)? Returns (allowed, reason) —
    reason is None when allowed, else the cap that refused it.
    Pure read + compute; never writes. Raises ValueError for an
    unknown action."""
    if action not in ACTION_KINDS:
        raise ValueError(
            f"unknown action {action!r} — use 'apply' or 'send'"
        )
    bounds = mission.get("bounds") or {}
    counts = _counts_today(conn, mission["id"], ist_day_start(now))
    kind_key = ACTION_KINDS[action]
    kind_cap_key = (
        "max_applies_per_day" if action == "apply"
        else "max_sends_per_day"
    )
    kind_cap = bounds.get(
        kind_cap_key,
        _DEFAULT_MAX_APPLIES if action == "apply" else _DEFAULT_MAX_SENDS,
    )
    if counts[kind_key] >= kind_cap:
        return False, (
            f"{kind_cap_key} reached: {counts[kind_key]}/{kind_cap} today"
        )
    total_cap = bounds.get("max_actions_per_day", _DEFAULT_MAX_ACTIONS)
    total = counts["applied"] + counts["sent"]
    if total >= total_cap:
        return False, (
            f"max_actions_per_day reached: {total}/{total_cap} today"
        )
    return True, None


def auto_approvable(conn, mission: dict, action: str,
                    now: int | None = None) -> tuple[bool, str | None]:
    """Q1 override — standing approval: may the runner
    auto-satisfy this action's pending approval, instead of
    waiting for the user? Only when ALL three hold:

    1. `auto_approve` is true on the mission, AND
    2. the action kind has been removed from
       `require_approval_for`, AND
    3. the action is within bounds/caps (check_bounds).

    Returns (allowed, reason). The reason distinguishes the
    three refusals: "auto_approve_off" (human approval needed),
    "requires_human_approval" (the kind still requires it — the
    runner leaves the pending approval for the user), or the cap
    reason from check_bounds (over cap — the runner refuses).
    Pure read + compute; never writes."""
    bounds = mission.get("bounds") or {}
    if not bounds.get("auto_approve", False):
        return False, "auto_approve_off"
    # NOTE: `or` would be wrong here — an EMPTY require list is
    # meaningful (no kind requires approval), and `[] or [...]`
    # would silently restore the default. Only a missing key
    # falls back.
    require = bounds.get("require_approval_for")
    if require is None:
        require = ["apply", "send"]
    if action in require:
        return False, "requires_human_approval"
    return check_bounds(conn, mission, action, now=now)


def default_bounds(mission_type: str) -> dict:
    """The registry defaults for a mission type (§3.5). Raises
    ValueError for an unknown type."""
    # Imported here, not at module level: types.py is W2's file
    # (see models.create_mission).
    from . import types as _types

    try:
        spec = _types.MISSION_TYPES[mission_type]
    except KeyError:
        raise ValueError(f"unknown mission type {mission_type!r}") from None
    return dict(spec.default_bounds)
