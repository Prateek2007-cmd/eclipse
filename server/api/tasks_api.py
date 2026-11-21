"""Task API (Sprint 3, plan §6.1). Token-authenticated, like the rest.

POST /tasks                              {goal, constraints?} -> 201 {id}
GET  /tasks/{id}                         -> task + steps + receipts
GET  /tasks                              -> list (filter by status)
POST /tasks/{id}/plan                    -> decompose the goal (1 planner call)
POST /tasks/{id}/steps/{n}/execute       {observation} -> one validated action
POST /tasks/{id}/steps/{n}/verify        {observation, expected_end_state, previous_url?}
                                         -> {verified, reason, retry}
POST /tasks/{id}/approve                 {approval_id} -> approve a pending step
POST /tasks/{id}/cancel | /pause | /resume
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..core import db
from ..core.auth import require_token
from ..planner import validate
from ..tasks import loop

router = APIRouter(tags=["tasks"])


class TaskIn(BaseModel):
    goal: str = Field(..., min_length=1, max_length=2000)
    constraints: str = Field(default="", max_length=2000)


class ObservationIn(BaseModel):
    url: str = Field(default="")
    elements: list[dict] = Field(default_factory=list)


class ExecuteIn(BaseModel):
    observation: ObservationIn


class VerifyIn(BaseModel):
    observation: ObservationIn
    # The pre-action observation: verify is a baseline diff, so the
    # caller must send the observation the step started from. Without
    # it, pre-existing text and pre-filled values would verify a step
    # that did nothing.
    previous_observation: ObservationIn
    expected_end_state: str = Field(default="", max_length=500)
    previous_url: str = Field(..., min_length=1, max_length=500)


def _conn():
    return db.connect()


def _task_or_404(task_id: str) -> dict:
    conn = _conn()
    try:
        task = loop.get_task(conn, task_id)
    finally:
        conn.close()
    if task is None:
        raise HTTPException(status_code=404, detail=f"no task {task_id}")
    return task


@router.post("/tasks", status_code=201)
def create_task(body: TaskIn, _token: str = Depends(require_token)):
    conn = _conn()
    try:
        task_id = loop.create_task(conn, body.goal, body.constraints)
    finally:
        conn.close()
    return {"id": task_id, "status": "created"}


@router.get("/tasks/{task_id}")
def get_task(task_id: str, _token: str = Depends(require_token)):
    task = _task_or_404(task_id)
    conn = _conn()
    try:
        steps = [
            dict(r)
            for r in conn.execute(
                "SELECT n, action_json, status, element_id, result, error, retries"
                " FROM steps WHERE task_id = ? ORDER BY n",
                (task_id,),
            ).fetchall()
        ]
        checkpoints = [
            dict(r)
            for r in conn.execute(
                "SELECT step_n, summary, created_at FROM checkpoints"
                " WHERE task_id = ? ORDER BY step_n",
                (task_id,),
            ).fetchall()
        ]
    finally:
        conn.close()
    return {**task, "steps": steps, "checkpoints": checkpoints}


@router.get("/tasks")
def list_tasks(status: str | None = None, _token: str = Depends(require_token)):
    conn = _conn()
    try:
        if status:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE status = ? ORDER BY created_at DESC",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC"
            ).fetchall()
    finally:
        conn.close()
    return {"tasks": [dict(r) for r in rows]}


@router.post("/tasks/{task_id}/plan")
def plan_task(task_id: str, _token: str = Depends(require_token)):
    _task_or_404(task_id)
    conn = _conn()
    try:
        steps = loop.plan_task(conn, task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except validate.ValidationError as exc:
        raise HTTPException(status_code=502, detail=f"planner plan invalid: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 - planner failures are 502s
        raise HTTPException(status_code=502, detail=f"planner failed: {exc}") from exc
    finally:
        conn.close()
    return {"task_id": task_id, "steps": steps}


@router.post("/tasks/{task_id}/steps/{n}/execute")
def execute_step(task_id: str, n: int, body: ExecuteIn, _token: str = Depends(require_token)):
    _task_or_404(task_id)
    conn = _conn()
    try:
        result = loop.execute_step(
            conn, task_id, n, body.observation.model_dump()
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except loop.GroundingError as exc:
        # The planner named an element that is not in the current
        # observation: the caller re-observes once and re-plans (§6.3).
        raise HTTPException(
            status_code=409, detail={"reobserve": True, "error": str(exc)}
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except validate.ValidationError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        conn.close()
    return result


@router.post("/tasks/{task_id}/steps/{n}/verify")
def verify_step(task_id: str, n: int, body: VerifyIn, _token: str = Depends(require_token)):
    _task_or_404(task_id)
    conn = _conn()
    try:
        result = loop.verify_step(
            conn,
            task_id,
            n,
            body.observation.model_dump(),
            body.expected_end_state,
            body.previous_url,
            body.previous_observation.model_dump(),
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        conn.close()
    return result


class ApproveIn(BaseModel):
    approval_id: str = Field(..., min_length=1, max_length=120)


@router.post("/tasks/{task_id}/approve")
def approve_step(task_id: str, body: ApproveIn, _token: str = Depends(require_token)):
    """Approve a pending step (the approval card flow, §5.4/§6.1).

    Records the approval: an audit entry and a grants row (scope
    "approve", 10-minute expiry), then unblocks the task. The
    failed step that triggered ask_user stays failed — the approval
    lets the task continue from the checkpoint, it does not re-run
    the failed step.
    """
    _task_or_404(task_id)
    conn = _conn()
    try:
        import time
        import uuid

        from ..core import audit

        now = int(time.time())
        audit.append(
            conn, task_id, "task.approve",
            {"approval_id": body.approval_id},
        )
        conn.execute(
            "INSERT INTO grants (id, origin, scope, expires_at, created_at)"
            " VALUES (?, ?, 'approve', ?, ?)",
            (uuid.uuid4().hex[:12], task_id, now + 600, now),
        )
        conn.execute(
            "UPDATE tasks SET status = 'planned', updated_at = ?"
            " WHERE id = ? AND status = 'ask_user'",
            (now, task_id),
        )
        conn.commit()
    finally:
        conn.close()
    return {"task_id": task_id, "status": "planned", "approval_id": body.approval_id}


def _set_status(task_id: str, status: str) -> dict:
    _task_or_404(task_id)
    conn = _conn()
    try:
        conn.execute(
            "UPDATE tasks SET status = ?, updated_at = strftime('%s','now') WHERE id = ?",
            (status, task_id),
        )
        conn.commit()
    finally:
        conn.close()
    return {"task_id": task_id, "status": status}


@router.post("/tasks/{task_id}/cancel")
def cancel_task(task_id: str, _token: str = Depends(require_token)):
    return _set_status(task_id, "cancelled")


@router.post("/tasks/{task_id}/pause")
def pause_task(task_id: str, _token: str = Depends(require_token)):
    return _set_status(task_id, "paused")


@router.post("/tasks/{task_id}/resume")
def resume_task(task_id: str, _token: str = Depends(require_token)):
    """Resume after a human-only handoff: the user completed the
    human step, so the handoff steps are marked done and the task
    continues. If the handoff deadline passed, the task fails."""
    _task_or_404(task_id)
    conn = _conn()
    try:
        status = loop.resume_task(conn, task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        conn.close()
    return {"task_id": task_id, "status": status}
