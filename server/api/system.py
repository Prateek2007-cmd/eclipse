"""System endpoints: health, metrics, quota. All token-authenticated."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ..core import auth, db

router = APIRouter(dependencies=[Depends(auth.require_token)], tags=["system"])


@router.get("/health")
def health() -> dict:
    """Liveness check. Token required, like every other endpoint."""
    return {"status": "ok", "service": "eclipse", "version": "0.1.0"}


@router.get("/metrics")
def metrics() -> dict:
    """Calls used, tasks, audit size. Token counts are 0 until the
    planner lands in Sprint 4."""
    conn = db.connect()
    try:
        tasks = conn.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"]
        audit = conn.execute(
            "SELECT COUNT(*) AS c FROM audit_log"
        ).fetchone()["c"]
        quota = conn.execute(
            "SELECT model, used_today, limit_day, reset_at FROM quota"
            " ORDER BY model"
        ).fetchall()
    finally:
        conn.close()
    return {
        "tasks": tasks,
        "audit_entries": audit,
        "quota": [dict(row) for row in quota],
        "tokens": {"in": 0, "out": 0},
        "note": "planner not built yet (Sprint 4); token counts are 0",
    }


@router.get("/quota")
def quota() -> dict:
    """Per-model calls used today, limit, and reset time."""
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT model, used_today, limit_day, reset_at FROM quota"
            " ORDER BY model"
        ).fetchall()
    finally:
        conn.close()
    return {"quota": [dict(row) for row in rows]}
