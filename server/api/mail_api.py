"""Cold-mail API (Sprint 5, plan Flow 2).

- POST /mail/drafts {task_id, recipients: [{email, role}]} →
  one draft per (recipient, role), deduped. The role comes from
  the shortlist (the match reasons are attached to the draft).
- GET /mail/drafts?task_id=&status= → the drafts.
- POST /mail/drafts/{id}/approve {approval_id} → the user read
  the draft and approved it (creates an approval grant, §5.4).
- PATCH /mail/drafts/{id} {body} → the user edits a draft.
- POST /mail/drafts/{id}/send → sends an approved draft (the
  mock transport until Sprint 7's Gmail connector) and logs the
  outcome per recruiter.
- GET /mail/log?task_id= → sent / bounced / no_reply per
  recruiter.
"""
from __future__ import annotations

import json
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..core import audit, db
from ..core.auth import require_token
from ..jobs import matcher, profile
from ..mail import drafts as draft_mod
from ..mail import send as send_mod
from ..tasks import loop

router = APIRouter()


def _conn():
    return db.connect()


class RecipientIn(BaseModel):
    email: str = Field(..., min_length=3, max_length=200)
    # The full role (from the shortlist), not just an id: the
    # draft is addressed to the real title and company, and the
    # match reasons come from the real role. A role without an id
    # is rejected — the dedup key needs one (no title fallback).
    role: dict = Field(..., min_length=1)


class DraftsIn(BaseModel):
    task_id: str = Field(..., min_length=1, max_length=100)
    recipients: list[RecipientIn] = Field(..., min_length=1, max_length=50)


class ApproveIn(BaseModel):
    approval_id: str = Field(..., min_length=1, max_length=120)


class EditIn(BaseModel):
    body: str = Field(..., min_length=1, max_length=10000)


@router.post("/mail/drafts")
def create_drafts(body: DraftsIn, _token: str = Depends(require_token)):
    """Draft one mail per (recipient, role). The role's match
    reasons come from the shortlist (the stored profile + the
    role, matched again). Dedup: an existing draft/mail for the
    pair is skipped and reported."""
    conn = _conn()
    try:
        if loop.get_task(conn, body.task_id) is None:
            raise HTTPException(status_code=404, detail=f"no task {body.task_id}")
        stored = profile.get_profile(conn)
        if stored is None:
            raise HTTPException(
                status_code=404, detail="no profile stored (share a resume first)"
            )
        created, skipped = [], []
        for recipient in body.recipients:
            role = recipient.role
            role_id = role.get("id") or role.get("url")
            if not role_id:
                skipped.append({"email": recipient.email, "role_id": None,
                                "reason": "role has no id"})
                continue
            # Only the shortlist gets mailed: a role that does not
            # fit is skipped and reported, never drafted.
            match = matcher.match_role(role, stored)
            if not match.get("fits"):
                skipped.append({"email": recipient.email, "role_id": role_id,
                                "reason": match.get("missing") or ["does not fit"]})
                continue
            draft_id = draft_mod.create_draft(
                conn, body.task_id, recipient.email, role, stored,
                match.get("reasons") or [],
            )
            if draft_id is None:
                skipped.append({"email": recipient.email, "role_id": role_id,
                                "reason": "already mailed"})
            else:
                created.append({"draft_id": draft_id, "email": recipient.email,
                                "role_id": role_id})
        return {"created": created, "skipped": skipped}
    finally:
        conn.close()


@router.get("/mail/drafts")
def get_drafts(task_id: str | None = None, status: str | None = None,
               _token: str = Depends(require_token)):
    conn = _conn()
    try:
        return {"drafts": draft_mod.list_drafts(conn, task_id, status)}
    finally:
        conn.close()


@router.post("/mail/drafts/{draft_id}/approve")
def approve_draft(draft_id: str, body: ApproveIn, _token: str = Depends(require_token)):
    """Approve a draft (the user read it). Creates an approval
    grant for the draft's task, so the send is authorised."""
    conn = _conn()
    try:
        draft = draft_mod.get_draft(conn, draft_id)
        if draft is None:
            raise HTTPException(status_code=404, detail=f"no draft {draft_id}")
        if not draft_mod.approve_draft(conn, draft_id):
            raise HTTPException(
                status_code=409, detail=f"draft {draft_id} is {draft['status']}"
            )
        now = int(time.time())
        audit.append(conn, draft["task_id"], "task.mail_approve",
                     {"approval_id": body.approval_id, "draft_id": draft_id})
        conn.execute(
            "INSERT INTO grants (id, origin, scope, expires_at, created_at, detail)"
            " VALUES (?, ?, 'approve', ?, ?, ?)",
            (uuid.uuid4().hex[:12], draft["task_id"], now + 600, now,
             json.dumps({"draft_id": draft_id})),
        )
        conn.commit()
    finally:
        conn.close()
    return {"draft_id": draft_id, "status": "approved"}


@router.patch("/mail/drafts/{draft_id}")
def edit_draft(draft_id: str, body: EditIn, _token: str = Depends(require_token)):
    conn = _conn()
    try:
        if draft_mod.get_draft(conn, draft_id) is None:
            raise HTTPException(status_code=404, detail=f"no draft {draft_id}")
        if not draft_mod.update_draft_body(conn, draft_id, body.body):
            raise HTTPException(status_code=409, detail="draft is already sent")
    finally:
        conn.close()
    return {"draft_id": draft_id, "status": "edited"}


@router.post("/mail/drafts/{draft_id}/send")
def send_draft(draft_id: str, _token: str = Depends(require_token)):
    """Send an approved draft. Requires a valid approval grant
    for the task (approval BEFORE the send, §5.4)."""
    conn = _conn()
    try:
        draft = draft_mod.get_draft(conn, draft_id)
        if draft is None:
            raise HTTPException(status_code=404, detail=f"no draft {draft_id}")
        # The send needs the approval grant for THIS draft (the
        # detail names the draft_id). One approval sends one draft:
        # the grant is consumed (deleted) here, so a single
        # approval cannot send every draft in the task, and a
        # job-submit approval cannot send a mail.
        now = int(time.time())
        grants = conn.execute(
            "SELECT id, detail FROM grants WHERE origin = ?"
            " AND scope = 'approve' AND expires_at > ?",
            (draft["task_id"], now),
        ).fetchall()
        grant_id = None
        for row in grants:
            try:
                detail = json.loads(row["detail"] or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            if detail.get("draft_id") == draft_id:
                grant_id = row["id"]
                break
        if grant_id is None:
            raise HTTPException(
                status_code=409,
                detail="no approval grant for this draft — approve it first",
            )
        cur = conn.execute("DELETE FROM grants WHERE id = ?", (grant_id,))
        if cur.rowcount != 1:
            raise HTTPException(
                status_code=409,
                detail="the approval was already used",
            )
        try:
            result = send_mod.send_draft(conn, draft_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"draft_id": draft_id, "status": result.status, "detail": result.detail}
    finally:
        conn.close()


@router.get("/mail/log")
def get_log(task_id: str | None = None, _token: str = Depends(require_token)):
    conn = _conn()
    try:
        return {"log": send_mod.mail_log(conn, task_id)}
    finally:
        conn.close()
