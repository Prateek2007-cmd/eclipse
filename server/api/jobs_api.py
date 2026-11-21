"""Job-hunt API (Sprint 4, plan Flow 1).

- POST /jobs/profile {resume_text} → parse the resume locally and
  store the profile (nothing leaves the device).
- GET /jobs/profile → the stored profile.
- POST /jobs/match {roles: [...]} → the shortlist with match
  reasons (fits, ranked) plus the excluded roles with their reasons.
- POST /tasks/{id}/receipts → record a receipt for a verified submit.
- GET /tasks/{id}/receipts → the task's receipts + the §6.9 report.
- GET /jobs/applied?role_id=&url= → whether the role was already
  applied to (receipts + the memory table).
"""
from __future__ import annotations

import datetime as _dt

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from ..core import db
from ..core.auth import require_token
from ..jobs import applied, matcher, profile, receipts
from ..tasks import loop

router = APIRouter()


def _conn():
    return db.connect()


class ProfileIn(BaseModel):
    resume_text: str = Field(..., min_length=10, max_length=20000)


class RoleIn(BaseModel):
    id: str = Field(..., min_length=1, max_length=200)
    title: str = Field(..., min_length=1, max_length=200)
    company: str = Field(default="", max_length=200)
    site: str = Field(default="", max_length=100)
    url: str = Field(default="", max_length=500)
    # A site may return "₹15,000" as a string; parse it.
    stipend: int | str | None = None
    location: str = Field(default="", max_length=100)
    deadline: str | None = None
    skills: list[str] = Field(default_factory=list)
    min_year: int | None = None
    degree: str | None = None
    min_cgpa: float | None = None
    requires_experience: bool = False

    @field_validator("stipend", mode="before")
    @classmethod
    def _parse_stipend(cls, value):
        if value is None or isinstance(value, int):
            return value
        text = str(value).replace("₹", "").replace(",", "").strip()
        if text == "":
            return None
        try:
            return int(float(text))
        except ValueError:
            raise ValueError(f"stipend is not a number: {value!r}")


class MatchIn(BaseModel):
    roles: list[RoleIn] = Field(..., min_length=1, max_length=200)


class ReceiptIn(BaseModel):
    role_id: str = Field(..., min_length=1, max_length=200)
    title: str = Field(..., min_length=1, max_length=200)
    company: str = Field(default="", max_length=200)
    url: str = Field(default="", max_length=500)
    confirmation: str = Field(..., min_length=1, max_length=2000)
    status: str = Field(default="submitted", pattern="^(submitted|failed)$")


@router.post("/jobs/profile")
def set_profile(body: ProfileIn, _token: str = Depends(require_token)):
    """Parse the resume locally and store the profile."""
    parsed = profile.parse_resume(body.resume_text)
    conn = _conn()
    try:
        profile.store_profile(conn, parsed)
    finally:
        conn.close()
    return {"profile": parsed}


@router.get("/jobs/profile")
def get_profile(_token: str = Depends(require_token)):
    conn = _conn()
    try:
        stored = profile.get_profile(conn)
    finally:
        conn.close()
    if stored is None:
        raise HTTPException(status_code=404, detail="no profile stored (share a resume first)")
    return {"profile": stored}


@router.post("/jobs/match")
def match_roles(body: MatchIn, _token: str = Depends(require_token)):
    """Match roles against the stored profile. Returns the shortlist
    (fits, ranked by score then deadline) and the excluded roles
    with the reason each was excluded."""
    conn = _conn()
    try:
        stored = profile.get_profile(conn)
    finally:
        conn.close()
    if stored is None:
        raise HTTPException(status_code=404, detail="no profile stored (share a resume first)")
    roles = [r.model_dump() for r in body.roles]
    today = _dt.date.today()
    results = [matcher.match_role(r, stored, today) for r in roles]
    fits = matcher.shortlist(roles, stored, today)
    excluded = [m for m in results if not m["fits"]]
    return {"shortlist": fits, "excluded": excluded, "count": len(roles)}


@router.post("/tasks/{task_id}/receipts")
def add_receipt(task_id: str, body: ReceiptIn, _token: str = Depends(require_token)):
    conn = _conn()
    try:
        if loop.get_task(conn, task_id) is None:
            raise HTTPException(status_code=404, detail=f"no task {task_id}")
        # A duplicate receipt for the same role is refused (§6.9 —
        # the loop already records receipts on verified submits,
        # so this endpoint is for the caller, not a second record).
        if applied.is_already_applied(conn, body.role_id, body.url or None):
            raise HTTPException(
                status_code=409,
                detail=f"already applied to {body.role_id} (receipt on file)",
            )
        receipt_id = receipts.record_receipt(
            conn, task_id, body.role_id, body.title, body.company,
            body.url, body.confirmation, body.status,
        )
        if body.status == "submitted":
            applied.mark_applied(conn, body.role_id, body.url)
    finally:
        conn.close()
    return {"receipt_id": receipt_id, "task_id": task_id, "status": body.status}


@router.get("/tasks/{task_id}/receipts")
def get_receipts(task_id: str, _token: str = Depends(require_token)):
    conn = _conn()
    try:
        if loop.get_task(conn, task_id) is None:
            raise HTTPException(status_code=404, detail=f"no task {task_id}")
        report = receipts.task_report(conn, task_id)
    finally:
        conn.close()
    return report


@router.get("/jobs/applied")
def check_applied(
    role_id: str = "", url: str = "", _token: str = Depends(require_token)
):
    if not role_id and not url:
        raise HTTPException(status_code=422, detail="role_id or url is required")
    conn = _conn()
    try:
        result = applied.is_already_applied(conn, role_id or url, url or None)
    finally:
        conn.close()
    return {"role_id": role_id, "url": url, "already_applied": result}
