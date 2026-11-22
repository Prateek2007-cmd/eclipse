"""Cold-mail API (Sprint 5, plan Flow 2; real Gmail delivery in
Sprint 7).

- POST /mail/drafts {task_id, recipients: [{email, role}]} →
  one draft per (recipient, role), deduped. The role comes from
  the shortlist (the match reasons are attached to the draft).
- GET /mail/drafts?task_id=&status= → the drafts.
- POST /mail/drafts/{id}/approve {approval_id} → the user read
  the draft and approved it (creates an approval grant, §5.4).
- PATCH /mail/drafts/{id} {body} → the user edits a draft.
- POST /mail/drafts/{id}/send {attach_resume=true} → sends an
  approved draft through the user's connected Gmail (Sprint 7)
  with the resume attached, and logs the outcome per recruiter.
- POST /mail/bounce-check?task_id= → checks the inbox for
  delivery failures for the task's sent mails and updates the
  log (a bounce arrives minutes after the send).
- GET /mail/log?task_id= → sent / bounced / no_reply per
  recruiter.
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from ..connectors import registry as conn_registry
from ..core import audit, db
from ..core.auth import require_token
from ..jobs import matcher, profile
from ..mail import drafts as draft_mod
from ..mail import send as send_mod
from ..tasks import loop

router = APIRouter()


def _conn():
    return db.connect()


def _find_resume(user_files: Path | None = None) -> str | None:
    """The user's resume PDF, for the cold-mail attachment.

    Env ECLIPSE_RESUME_PATH wins; otherwise the newest PDF in
    ~/workspace/user/files whose name starts with the user's
    first name ("Akash") or contains "resume". The name rule
    matters: the folder also holds other people's documents
    (e.g. Harika's ServiceNow interview write-up), and attaching
    another person's file to a recruiter would be a live
    mistake. None when no file matches — the caller must not
    silently send without the attachment it promised.

    A file NAMED as a resume wins over a merely same-owner file:
    the folder also holds the user's other documents (a ServiceNow
    write-up, interview notes), and the newest of those must not
    be attached as the resume. Within a class, the newest wins.

    The local file is the canonical attachment source (it is the
    resume the user handed over). Drive's find_resume() is a
    separate lookup and may hold a different copy; the two are
    not compared here.
    """
    env = os.environ.get("ECLIPSE_RESUME_PATH")
    if env and Path(env).is_file():
        return env
    user_files = user_files or (Path.home() / "workspace" / "user" / "files")
    if not user_files.is_dir():
        return None
    candidates = [
        p for p in user_files.iterdir()
        if p.is_file() and p.suffix.lower() == ".pdf"
        and (p.name.lower().startswith("akash")
             or "resume" in p.name.lower())
    ]
    if not candidates:
        return None

    def _rank(p):
        name = p.name.lower()
        named = 0 if ("resume" in name
                      or re.search(r"(^|[^a-z])cv([^a-z]|$)", name)) else 1
        return (named, -p.stat().st_mtime)

    candidates.sort(key=_rank)
    return str(candidates[0])


_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def _live_grant_for_draft(conn, task_id: str, draft_id: str) -> str | None:
    """The id of a live approval grant naming this draft, or None.
    One definition, used by the approve endpoint (is this draft
    already approved with a live grant?) and the send endpoint
    (which grant does this send consume?)."""
    now = int(time.time())
    rows = conn.execute(
        "SELECT id, detail FROM grants WHERE origin = ?"
        " AND scope = 'approve' AND expires_at > ?",
        (task_id, now),
    ).fetchall()
    for row in rows:
        try:
            detail = json.loads(row["detail"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if detail.get("draft_id") == draft_id:
            return row["id"]
    return None


def _revoke_grants_for_draft(conn, task_id: str, draft_id: str) -> None:
    """Delete every approval grant naming this draft — an edit
    revokes the approval the grant carries, so a stale grant
    must not survive to send the edited body."""
    rows = conn.execute(
        "SELECT id, detail FROM grants WHERE origin = ? AND scope = 'approve'",
        (task_id,),
    ).fetchall()
    for row in rows:
        try:
            detail = json.loads(row["detail"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if detail.get("draft_id") == draft_id:
            conn.execute("DELETE FROM grants WHERE id = ?", (row["id"],))
    conn.commit()


class RecipientIn(BaseModel):
    email: str = Field(..., min_length=3, max_length=200)
    # The full role (from the shortlist), not just an id: the
    # draft is addressed to the real title and company, and the
    # match reasons come from the real role. A role without an id
    # is rejected — the dedup key needs one (no title fallback).
    role: dict = Field(..., min_length=1)

    @field_validator("email")
    @classmethod
    def _check_email(cls, value: str) -> str:
        if not _EMAIL_RE.match(value):
            raise ValueError(f"not an email address: {value}")
        return value


class DraftsIn(BaseModel):
    task_id: str = Field(..., min_length=1, max_length=100)
    recipients: list[RecipientIn] = Field(..., min_length=1, max_length=50)


class ApproveIn(BaseModel):
    approval_id: str = Field(..., min_length=1, max_length=120)


class EditIn(BaseModel):
    body: str = Field(..., min_length=1, max_length=10000)


class SendIn(BaseModel):
    # Attach the resume PDF to the mail. Default False: the
    # approved draft never promises an attachment ("I can share
    # my resume"), so attaching one is a per-send opt-in, not
    # something the approval silently expands to. When True and
    # no resume file is found, the send is refused (409) rather
    # than silently going out without the attachment.
    attach_resume: bool = False


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
        if draft["status"] == "approved":
            # The draft was approved but its grant may have
            # expired (grants live 10 minutes) — without a live
            # grant the draft is stuck: approved but un-sendable,
            # and approve_draft refuses a second approval. Allow
            # a fresh approval when no live grant names it (a
            # failed send lands here too: the failure is logged,
            # the draft stays approved, and the user re-approves
            # to retry).
            if _live_grant_for_draft(conn, draft["task_id"], draft_id) is not None:
                raise HTTPException(
                    status_code=409, detail=f"draft {draft_id} is approved"
                )
        elif not draft_mod.approve_draft(conn, draft_id):
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
        draft = draft_mod.get_draft(conn, draft_id)
        if draft is None:
            raise HTTPException(status_code=404, detail=f"no draft {draft_id}")
        if not draft_mod.update_draft_body(conn, draft_id, body.body):
            raise HTTPException(status_code=409, detail="draft is already sent")
        # The edit revoked the approval (the draft is a draft
        # again) — drop any grant naming it, so a stale approval
        # cannot send the edited body.
        _revoke_grants_for_draft(conn, draft["task_id"], draft_id)
    finally:
        conn.close()
    return {"draft_id": draft_id, "status": "edited"}


@router.post("/mail/drafts/{draft_id}/send")
def send_draft(draft_id: str, body: SendIn | None = None,
               _token: str = Depends(require_token)):
    """Send an approved draft through the user's connected Gmail.
    Requires a valid approval grant for the task (approval BEFORE
    the send, §5.4). The resume PDF is attached when found."""
    conn = _conn()
    try:
        draft = draft_mod.get_draft(conn, draft_id)
        if draft is None:
            raise HTTPException(status_code=404, detail=f"no draft {draft_id}")
        # Gmail must be on and connected — a disabled connector
        # or a revoked grant stops the send, it never falls back
        # to an unapproved channel. Fail closed: a missing
        # registry row is not "on".
        reg = conn_registry.get(conn, "gmail")
        if reg is None or not reg["enabled"]:
            raise HTTPException(
                status_code=503, detail="Gmail connector is not available"
            )
        # The send needs the approval grant for THIS draft (the
        # detail names the draft_id). One approval sends one draft:
        # the grant is consumed (deleted) here, so a single
        # approval cannot send every draft in the task, and a
        # job-submit approval cannot send a mail.
        #
        # Atomicity note: the DELETE below is uncommitted until
        # send_draft commits. A send refused before the transport
        # runs (a bounced address, a draft that is no longer
        # approved) rolls the grant back (sqlite3 rolls back the
        # open transaction on close), so the approval survives
        # and can be retried. A transport failure is different:
        # send_draft logs the failure and commits, which also
        # commits the grant consumption — the approval
        # authorised one send attempt, and the attempt happened.
        # The draft stays approved and can be re-approved for a
        # retry. The open write transaction is held across the
        # send call, so a concurrent same-draft send hits the
        # busy timeout — that surfaces as 409, not a half-sent
        # mail.
        grant_id = _live_grant_for_draft(conn, draft["task_id"], draft_id)
        if grant_id is None:
            raise HTTPException(
                status_code=409,
                detail="no approval grant for this draft — approve it first",
            )
        import sqlite3

        try:
            cur = conn.execute("DELETE FROM grants WHERE id = ?", (grant_id,))
        except sqlite3.OperationalError:
            raise HTTPException(
                status_code=409, detail="the approval was already used"
            ) from None
        if cur.rowcount != 1:
            raise HTTPException(
                status_code=409,
                detail="the approval was already used",
            )
        attach = None
        if body is not None and body.attach_resume:
            attach = _find_resume()
            if attach is None:
                conn.rollback()
                raise HTTPException(
                    status_code=409,
                    detail="attach_resume was requested but no resume file was found",
                )
        try:
            result = send_mod.send_draft(conn, draft_id, attach=attach)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - connector errors
            from server.connectors.base import ConnectorError

            if isinstance(exc, ConnectorError) and exc.kind == "auth":
                from server.connectors.gmail import GmailConnector

                live = GmailConnector().status()
                raise HTTPException(
                    status_code=503,
                    detail={
                        "error": "the Google grant expired or was revoked",
                        "state": "needs_reconnect",
                        "connect_url": live.get("connect_url"),
                    },
                ) from exc
            raise HTTPException(status_code=502, detail=f"Gmail send failed: {exc}") from exc
        return {
            "draft_id": draft_id,
            "status": result.status,
            "detail": result.detail,
            "attached_resume": Path(attach).name if attach else None,
        }
    finally:
        conn.close()


class BounceCheckIn(BaseModel):
    task_id: str = Field(..., min_length=1, max_length=100)


@router.post("/mail/bounce-check")
def bounce_check(body: BounceCheckIn, _token: str = Depends(require_token)):
    """Check the inbox for delivery failures for the task's sent
    mails. A bounce arrives minutes after the send, so this is a
    separate check — it updates any sent mail that bounced."""
    conn = _conn()
    try:
        if loop.get_task(conn, body.task_id) is None:
            raise HTTPException(status_code=404, detail=f"no task {body.task_id}")
        reg = conn_registry.get(conn, "gmail")
        if reg is None or not reg["enabled"]:
            raise HTTPException(
                status_code=503, detail="Gmail connector is not available"
            )
        try:
            results = send_mod.check_bounces_for_task(conn, body.task_id)
        except Exception as exc:  # noqa: BLE001 - connector errors
            raise HTTPException(
                status_code=502, detail=f"bounce check failed: {exc}"
            ) from exc
        return {"task_id": body.task_id, "checked": results}
    finally:
        conn.close()


@router.get("/mail/log")
def get_log(task_id: str | None = None, _token: str = Depends(require_token)):
    conn = _conn()
    try:
        return {"log": send_mod.mail_log(conn, task_id)}
    finally:
        conn.close()
