"""Connectors API (Sprint 7, plan §7).

- GET /connectors → the three connectors with on/off + last
  known state.
- GET /connectors/{name}/status → a LIVE check via the CLI
  (connected / needs_reconnect + connect_url / unavailable).
  Records the result in the registry.
- POST /connectors/{name}/enable → turn a connector on.
- POST /connectors/{name}/disable → turn a connector off
  (Eclipse stops using it; the Google grant is revoked in the
  platform Settings, never here).
- POST /connectors/approvals {task_id, kind, summary?, start?,
  path?} → the user read the plain-words card and approved one
  connector action. Returns {approval_id} (a single-use grant,
  10-minute expiry, detail names the action).
- GET /connectors/gmail/offers → offer/interview mails from the
  inbox, bodies and subjects redacted before they reach the caller.
- GET /connectors/calendar/freebusy?start=&end= → free/busy.
- POST /connectors/calendar/events {task_id, approval_id,
  summary, start, end, description, location} → create an event.
  The approval grant is consumed only on success; a failed
  create leaves it usable for a retry. Re-posting the same
  (task, summary, start) returns the existing event, never a
  duplicate.
- GET /connectors/drive/resume → the resume file in Drive.
- POST /connectors/drive/upload {task_id, approval_id, path,
  name} → upload a file the user asked for (receipts, audit
  exports, briefing). The path must be absolute and inside the
  home directory.

All routes are token-gated. A connector that is off returns
503; an expired/revoked grant returns 503 with the connect_url
(the reconnect card case, §7.4); a rate limit returns 429; other
connector failures return 502 with a short generic detail — the
full CLI output stays in the server log only.
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from ..connectors import registry as conn_registry
from ..connectors.base import ConnectorError, public_error
from ..connectors.calendar import CalendarConnector
from ..connectors.drive import DriveConnector
from ..connectors.gmail import GmailConnector
from ..core import audit, db
from ..core.auth import require_token
from ..tasks import loop

router = APIRouter()

_CONNECTORS = {
    "gmail": GmailConnector,
    "calendar": CalendarConnector,
    "drive": DriveConnector,
}

APPROVAL_TTL_S = 600


def _conn():
    return db.connect()


def _task_or_404(conn, task_id: str) -> None:
    if loop.get_task(conn, task_id) is None:
        raise HTTPException(status_code=404, detail=f"no task {task_id}")


def _require_on(conn, name: str) -> dict:
    """The registry row for a connector that exists and is on.
    Fail closed: a missing row is not "on"."""
    reg = conn_registry.get(conn, name)
    if reg is None:
        raise HTTPException(
            status_code=503, detail=f"{name} connector is not registered"
        )
    if not reg["enabled"]:
        raise HTTPException(
            status_code=503, detail=f"{name} connector is off — enable it first"
        )
    return reg


def _live_status(name: str) -> dict:
    """A live status check through the CLI."""
    try:
        return _CONNECTORS[name]().status()
    except Exception as exc:  # noqa: BLE001 - CLI shape varies
        return {"name": name, "state": "unavailable", "error": str(exc)}


def _connector_http_error(name: str, exc: ConnectorError) -> HTTPException:
    """Map a ConnectorError to the right HTTP status. Auth
    failures are the reconnect-card case (§7.4): 503 with the
    connect_url, so the caller can show the card."""
    if exc.kind == "auth":
        live = _live_status(name)
        return HTTPException(
            status_code=503,
            detail={
                "error": public_error(exc),
                "state": "needs_reconnect",
                "connect_url": live.get("connect_url"),
            },
        )
    if exc.kind == "quota":
        return HTTPException(status_code=429, detail=public_error(exc))
    return HTTPException(status_code=502, detail=public_error(exc))


def _rfc3339(value: str) -> str:
    """Validate an RFC3339 timestamp (tz-aware)."""
    from datetime import datetime

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"not an RFC3339 timestamp: {value}") from None
    if parsed.tzinfo is None:
        raise ValueError(f"RFC3339 timestamp needs a timezone offset: {value}")
    return value


def _consume_approval(conn, task_id: str, approval_id: str,
                      kind: str, match: dict) -> None:
    """Consume the single-use approval grant for this action.

    The grant must name this kind and match the action's
    identifying fields. The DELETE is issued here; the caller
    commits only after the connector call succeeds, so a failed
    create leaves the approval usable for a retry.
    """
    now = int(time.time())
    row = conn.execute(
        "SELECT id, detail FROM grants WHERE id = ? AND origin = ?"
        " AND scope = 'approve' AND expires_at > ?",
        (approval_id, task_id, now),
    ).fetchone()
    if row is None:
        raise HTTPException(
            status_code=409,
            detail="no valid approval for this action — approve it first",
        )
    try:
        detail = json.loads(row["detail"] or "{}")
    except (json.JSONDecodeError, TypeError):
        detail = {}
    if detail.get("kind") != kind:
        raise HTTPException(
            status_code=409,
            detail=f"this approval is for a {detail.get('kind')}, not a {kind}",
        )
    for key, value in match.items():
        if detail.get(key) != value:
            raise HTTPException(
                status_code=409,
                detail="this approval names a different action",
            )
    cur = conn.execute("DELETE FROM grants WHERE id = ?", (approval_id,))
    if cur.rowcount != 1:
        raise HTTPException(status_code=409, detail="the approval was already used")


@router.get("/connectors")
def list_connectors(_token: str = Depends(require_token)):
    conn = _conn()
    try:
        return {"connectors": conn_registry.get_all(conn)}
    finally:
        conn.close()


@router.get("/connectors/{name}/status")
def connector_status(name: str, _token: str = Depends(require_token)):
    if name not in _CONNECTORS:
        raise HTTPException(status_code=404, detail=f"no connector {name}")
    conn = _conn()
    try:
        live = _live_status(name)
        conn_registry.record_status(
            conn, name, live["state"], live.get("error")
        )
        return live
    finally:
        conn.close()


@router.post("/connectors/{name}/enable")
def enable_connector(name: str, _token: str = Depends(require_token)):
    conn = _conn()
    try:
        row = conn_registry.set_enabled(conn, name, True)
        if row is None:
            raise HTTPException(status_code=404, detail=f"no connector {name}")
        return row
    finally:
        conn.close()


@router.post("/connectors/{name}/disable")
def disable_connector(name: str, _token: str = Depends(require_token)):
    conn = _conn()
    try:
        row = conn_registry.set_enabled(conn, name, False)
        if row is None:
            raise HTTPException(status_code=404, detail=f"no connector {name}")
        return row
    finally:
        conn.close()


class ApprovalIn(BaseModel):
    task_id: str = Field(..., min_length=1, max_length=100)
    kind: str = Field(..., pattern="^(calendar_event|drive_upload)$")
    summary: str | None = Field(default=None, max_length=200)
    start: str | None = None
    path: str | None = None


@router.post("/connectors/approvals")
def create_approval(body: ApprovalIn, _token: str = Depends(require_token)):
    """The user read the plain-words card and approved one
    connector action. Returns the single-use approval id."""
    conn = _conn()
    try:
        _task_or_404(conn, body.task_id)
        if body.kind == "calendar_event" and not (body.summary and body.start):
            raise HTTPException(
                status_code=422,
                detail="a calendar approval needs summary and start",
            )
        if body.kind == "drive_upload" and not body.path:
            raise HTTPException(
                status_code=422, detail="a Drive approval needs path"
            )
        # The approval names the canonical path: the upload
        # endpoint resolves the path before matching it, so an
        # approval stored with `..` or a symlink in it could
        # never be consumed. Validating here also fails fast on
        # a path outside the home directory.
        path = (
            _validate_upload_path(body.path)
            if body.kind == "drive_upload"
            else body.path
        )
        approval_id = f"appr_{uuid.uuid4().hex[:12]}"
        now = int(time.time())
        detail = {"kind": body.kind}
        if body.summary is not None:
            detail["summary"] = body.summary
        if body.start is not None:
            detail["start"] = body.start
        if path is not None:
            detail["path"] = path
        conn.execute(
            "INSERT INTO grants (id, origin, scope, expires_at, created_at, detail)"
            " VALUES (?, ?, 'approve', ?, ?, ?)",
            (approval_id, body.task_id, now + APPROVAL_TTL_S, now,
             json.dumps(detail)),
        )
        audit.append(
            conn, body.task_id, "connector.approve",
            {"approval_id": approval_id, "kind": body.kind},
        )
        conn.commit()
        return {"approval_id": approval_id, "expires_at": now + APPROVAL_TTL_S}
    finally:
        conn.close()


@router.get("/connectors/gmail/offers")
def gmail_offers(
    max_results: int = Query(20, ge=1, le=100),
    _token: str = Depends(require_token),
):
    conn = _conn()
    try:
        _require_on(conn, "gmail")
        try:
            return {"offers": GmailConnector().search_offers(max_results)}
        except ConnectorError as exc:
            raise _connector_http_error("gmail", exc) from exc
    finally:
        conn.close()


@router.get("/connectors/calendar/freebusy")
def calendar_freebusy(
    start: str, end: str, _token: str = Depends(require_token)
):
    try:
        start = _rfc3339(start)
        end = _rfc3339(end)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    conn = _conn()
    try:
        _require_on(conn, "calendar")
        try:
            data = CalendarConnector().freebusy(start, end)
        except ConnectorError as exc:
            raise _connector_http_error("calendar", exc) from exc
        # Fail closed: a response without the free/busy block is
        # a malformed response, not a free slot.
        calendars = data.get("calendars")
        if not isinstance(calendars, dict) or "primary" not in calendars:
            raise HTTPException(
                status_code=502,
                detail="the calendar call returned no free/busy data",
            )
        primary = calendars["primary"]
        if not isinstance(primary, dict):
            raise HTTPException(
                status_code=502,
                detail="the calendar call returned no free/busy data",
            )
        busy = primary.get("busy") or []
        if not isinstance(busy, list):
            raise HTTPException(
                status_code=502,
                detail="the calendar call returned no free/busy data",
            )
        return {"free": len(busy) == 0, "busy": busy}
    finally:
        conn.close()


class EventIn(BaseModel):
    task_id: str = Field(..., min_length=1, max_length=100)
    approval_id: str = Field(..., min_length=1, max_length=120)
    summary: str = Field(..., min_length=1, max_length=200)
    start: str = Field(..., min_length=1)
    end: str = Field(..., min_length=1)
    description: str = Field(default="", max_length=5000)
    location: str = Field(default="", max_length=500)

    @field_validator("start", "end")
    @classmethod
    def _check_rfc3339(cls, value: str) -> str:
        return _rfc3339(value)

    @field_validator("end")
    @classmethod
    def _check_end_after_start(cls, value: str, info) -> str:
        from datetime import datetime

        start = info.data.get("start")
        if start:
            start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if end_dt <= start_dt:
                raise ValueError("end must be after start")
        return value


@router.post("/connectors/calendar/events")
def create_event(body: EventIn, _token: str = Depends(require_token)):
    """Create an interview/deadline event. Requires a
    single-use approval for this exact event (summary + start);
    the approval is consumed only when the create succeeds."""
    conn = _conn()
    try:
        _task_or_404(conn, body.task_id)
        _require_on(conn, "calendar")
        _consume_approval(
            conn, body.task_id, body.approval_id, "calendar_event",
            {"summary": body.summary, "start": body.start},
        )
        # Idempotency: the same (task, summary, start) never
        # creates a second Google event.
        existing = conn.execute(
            "SELECT event_id, summary, start, end, location FROM calendar_events"
            " WHERE task_id = ? AND summary = ? AND start = ?",
            (body.task_id, body.summary, body.start),
        ).fetchone()
        if existing is not None:
            conn.commit()  # the grant DELETE lands with the dedup return
            return {
                "id": existing["event_id"],
                "summary": existing["summary"],
                "start": existing["start"],
                "end": existing["end"],
                "htmlLink": None,
                "duplicate": True,
            }
        try:
            event = CalendarConnector().create_event(
                body.summary, body.start, body.end,
                description=body.description, location=body.location,
            )
        except ConnectorError as exc:
            conn.rollback()
            raise _connector_http_error("calendar", exc) from exc
        conn.execute(
            "INSERT INTO calendar_events (id, task_id, event_id, summary,"
            " start, end, location, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"ev_{uuid.uuid4().hex[:12]}",
                body.task_id,
                event.get("id"),
                body.summary,
                body.start,
                body.end,
                body.location,
                int(time.time()),
            ),
        )
        audit.append(
            conn, body.task_id, "connector.calendar_event",
            {"event_id": event.get("id"), "summary": body.summary,
             "start": body.start},
        )
        conn.commit()
        return event
    finally:
        conn.close()


@router.get("/connectors/drive/resume")
def drive_resume(_token: str = Depends(require_token)):
    conn = _conn()
    try:
        _require_on(conn, "drive")
        try:
            resume = DriveConnector().find_resume()
        except ConnectorError as exc:
            raise _connector_http_error("drive", exc) from exc
        if resume is None:
            raise HTTPException(status_code=404, detail="no resume found in Drive")
        return resume
    finally:
        conn.close()


class UploadIn(BaseModel):
    task_id: str = Field(..., min_length=1, max_length=100)
    approval_id: str = Field(..., min_length=1, max_length=120)
    path: str = Field(..., min_length=1)
    name: str | None = None


def _validate_upload_path(path: str) -> str:
    """The upload path must be absolute, inside the home
    directory, and not an option flag. The CLI is the backstop,
    not the check."""
    if path.startswith("-"):
        raise HTTPException(status_code=422, detail="the path must not start with -")
    candidate = Path(path)
    if not candidate.is_absolute():
        raise HTTPException(status_code=422, detail="the path must be absolute")
    try:
        candidate.resolve().relative_to(Path.home().resolve())
    except ValueError:
        raise HTTPException(
            status_code=422, detail="the path must be inside the home directory"
        ) from None
    return str(candidate.resolve())


@router.post("/connectors/drive/upload")
def drive_upload(body: UploadIn, _token: str = Depends(require_token)):
    """Upload a file the user asked for (receipt, audit export,
    briefing). Requires a single-use approval naming this path;
    the approval is consumed only when the upload succeeds."""
    conn = _conn()
    try:
        _task_or_404(conn, body.task_id)
        _require_on(conn, "drive")
        path = _validate_upload_path(body.path)
        _consume_approval(
            conn, body.task_id, body.approval_id, "drive_upload",
            {"path": path},
        )
        try:
            result = DriveConnector().upload_file(path, name=body.name)
        except ConnectorError as exc:
            conn.rollback()
            raise _connector_http_error("drive", exc) from exc
        audit.append(
            conn, body.task_id, "connector.drive_upload",
            {"file_id": result.get("id"), "path": path},
        )
        conn.commit()
        return result
    finally:
        conn.close()
