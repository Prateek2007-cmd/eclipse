"""Connector registry (Sprint 7, plan §7.4).

Per-connector on/off lives in Eclipse's SQLite (the `connectors`
table). Turning a connector off stops Eclipse using it — it does
NOT revoke the Google grant; revoking happens in the platform
Settings via the disconnect URL. The registry also records the
last live status check, so the workspace can show "needs
reconnect" without calling the CLI on every page load.
"""
from __future__ import annotations

import time

CONNECTORS = ("gmail", "calendar", "drive")


def get_all(conn) -> list[dict]:
    """All three connectors with their on/off flag and last status."""
    rows = conn.execute(
        "SELECT name, enabled, state, last_checked, last_error"
        " FROM connectors ORDER BY name"
    ).fetchall()
    out = []
    for row in rows:
        out.append(
            {
                "name": row["name"],
                "enabled": bool(row["enabled"]),
                "state": row["state"],
                "last_checked": row["last_checked"],
                "last_error": row["last_error"],
            }
        )
    return out


def get(conn, name: str) -> dict | None:
    """One connector's registry row, or None for an unknown name."""
    if name not in CONNECTORS:
        return None
    row = conn.execute(
        "SELECT name, enabled, state, last_checked, last_error"
        " FROM connectors WHERE name = ?",
        (name,),
    ).fetchone()
    if row is None:
        return None
    return {
        "name": row["name"],
        "enabled": bool(row["enabled"]),
        "state": row["state"],
        "last_checked": row["last_checked"],
        "last_error": row["last_error"],
    }


def set_enabled(conn, name: str, enabled: bool) -> dict | None:
    """Turn a connector on or off. Returns the updated row."""
    if name not in CONNECTORS:
        return None
    conn.execute(
        "UPDATE connectors SET enabled = ? WHERE name = ?",
        (1 if enabled else 0, name),
    )
    conn.commit()
    return get(conn, name)


def record_status(conn, name: str, state: str, error: str | None = None) -> None:
    """Record the result of a live status check."""
    if name not in CONNECTORS:
        return
    conn.execute(
        "UPDATE connectors SET state = ?, last_checked = ?, last_error = ?"
        " WHERE name = ?",
        (state, int(time.time()), error, name),
    )
    conn.commit()
