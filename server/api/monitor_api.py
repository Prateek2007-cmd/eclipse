"""Live monitoring API (Sprint 8, plan §8).

- POST /monitor/watches {url, selector, condition, interval_min}
  → create a watch (interval ≥ 15 min).
- GET /monitor/watches?status= → list watches.
- GET /monitor/watches/{id} → one watch.
- POST /monitor/watches/{id}/mute {minutes} → mute (reversible).
- POST /monitor/watches/{id}/unmute → unmute.
- DELETE /monitor/watches/{id} → delete.
- GET /monitor/due → watches due for a check (the extension
  alarm calls this, then checks each one in an open tab).
- POST /monitor/watches/{id}/check {found, value} → evaluate a
  fresh observation. A firing is written to the audit log.
- GET /monitor/events?watch_id=&limit= → the firing log.
- POST /monitor/briefing → build the morning briefing (watch
  firings + mission news + one Gmail scan). At most one build
  per IST day unless {"force": true} is posted. GET
  /monitor/briefing/latest → the newest stored briefing.

All routes are token-gated. Watches only run while Chrome is
open (chrome.alarms); the server never polls a page by itself.

Response envelopes (N2): single resources are returned bare
(a watch, a check result, a briefing); lists are wrapped
({watches: [...]}, {events: [...]}). This matches the rest of
the Eclipse API (tasks are bare, mail drafts are wrapped).
"""
from __future__ import annotations

from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator

from ..core import audit, db
from ..core.auth import require_token
from ..monitor import briefing as briefing_mod
from ..monitor import watches as watch_mod

router = APIRouter()

WATCH_STATUSES = ("active", "muted", "broken")


def _validate_watch_url(url: str) -> str:
    """M1: a watch URL must be an http(s) URL with a host — the
    extension does `new URL(url)` and builds a tab query from
    it, so a relative path or a bare word breaks the alarm run."""
    url = url.strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(
            f"url must be an http(s) URL with a host, got {url!r}"
        )
    return url


def _conn():
    return db.connect()


class WatchIn(BaseModel):
    url: str = Field(..., min_length=1, max_length=2000)
    selector: str = Field(..., min_length=1, max_length=500)
    condition: str = Field(..., min_length=1, max_length=500)
    interval_min: int = Field(default=60, ge=15, le=1440)

    _url_ok = field_validator("url", mode="after")(_validate_watch_url)


class RepairIn(BaseModel):
    selector: str | None = Field(default=None, min_length=1, max_length=500)
    url: str | None = Field(default=None, min_length=1, max_length=2000)

    @field_validator("url", mode="after")
    @classmethod
    def _url_ok(cls, v):
        return _validate_watch_url(v) if v is not None else None


class CheckIn(BaseModel):
    found: bool
    value: str | None = Field(default=None, max_length=10000)

    @model_validator(mode="after")
    def _found_needs_a_value(self):
        # N8: found=True with value=None would fire spuriously
        # (None != last_value) and then reset the baseline.
        if self.found and self.value is None:
            raise ValueError("found=true needs a value")
        return self


class MuteIn(BaseModel):
    minutes: int = Field(default=60, ge=1, le=1440)


class BriefingIn(BaseModel):
    # PR #5: without force, build_briefing returns the stored
    # briefing when one was already built today (IST) — the
    # server tick and the extension alarm both trigger a build,
    # and the day guard makes the double trigger harmless.
    # force=True (the workspace "Build now" button) always builds.
    force: bool = False


@router.post("/monitor/watches")
def create_watch(body: WatchIn, _token: str = Depends(require_token)):
    conn = _conn()
    try:
        try:
            watch = watch_mod.create_watch(
                conn, body.url, body.selector, body.condition,
                interval_min=body.interval_min,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return watch
    finally:
        conn.close()


@router.get("/monitor/watches")
def list_watches(status: str | None = None, _token: str = Depends(require_token)):
    if status is not None and status not in WATCH_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"status must be one of {', '.join(WATCH_STATUSES)}",
        )
    conn = _conn()
    try:
        return {"watches": watch_mod.list_watches(conn, status)}
    finally:
        conn.close()


@router.get("/monitor/watches/{watch_id}")
def get_watch(watch_id: str, _token: str = Depends(require_token)):
    conn = _conn()
    try:
        watch = watch_mod.get_watch(conn, watch_id)
        if watch is None:
            raise HTTPException(status_code=404, detail=f"no watch {watch_id}")
        return watch
    finally:
        conn.close()


@router.post("/monitor/watches/{watch_id}/mute")
def mute_watch(watch_id: str, body: MuteIn | None = None,
               _token: str = Depends(require_token)):
    conn = _conn()
    try:
        minutes = body.minutes if body is not None else 60
        watch = watch_mod.mute_watch(conn, watch_id, minutes)
        if watch is None:
            raise HTTPException(status_code=404, detail=f"no watch {watch_id}")
        return watch
    finally:
        conn.close()


@router.post("/monitor/watches/{watch_id}/unmute")
def unmute_watch(watch_id: str, _token: str = Depends(require_token)):
    conn = _conn()
    try:
        watch = watch_mod.unmute_watch(conn, watch_id)
        if watch is None:
            raise HTTPException(status_code=404, detail=f"no watch {watch_id}")
        return watch
    finally:
        conn.close()


@router.patch("/monitor/watches/{watch_id}")
def repair_watch(watch_id: str, body: RepairIn,
                 _token: str = Depends(require_token)):
    """Re-pick the element a broken watch follows (M3, §8.3):
    update the selector (and optionally the URL) and restart
    the watch from a fresh baseline."""
    conn = _conn()
    try:
        if body.selector is None and body.url is None:
            raise HTTPException(
                status_code=422, detail="give a selector or a url to repair"
            )
        try:
            watch = watch_mod.repair_watch(
                conn, watch_id, selector=body.selector, url=body.url
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if watch is None:
            raise HTTPException(status_code=404, detail=f"no watch {watch_id}")
        return watch
    finally:
        conn.close()


@router.delete("/monitor/watches/{watch_id}")
def delete_watch(watch_id: str, _token: str = Depends(require_token)):
    conn = _conn()
    try:
        if not watch_mod.delete_watch(conn, watch_id):
            raise HTTPException(status_code=404, detail=f"no watch {watch_id}")
        return {"deleted": watch_id}
    finally:
        conn.close()


@router.get("/monitor/due")
def due_watches(_token: str = Depends(require_token)):
    conn = _conn()
    try:
        return {"watches": watch_mod.due_watches(conn)}
    finally:
        conn.close()


@router.post("/monitor/watches/{watch_id}/check")
def check_watch(watch_id: str, body: CheckIn,
                _token: str = Depends(require_token)):
    conn = _conn()
    try:
        try:
            result = watch_mod.check_watch(
                conn, watch_id, body.found, body.value
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if result["fired"]:
            audit.append(
                conn, None, "monitor.watch_fired",
                {"watch_id": watch_id, "reason": result["reason"]},
            )
        elif result.get("became_broken"):
            # M5: one audit entry per break, not one per repeat
            # miss on an already-broken watch.
            audit.append(
                conn, None, "monitor.watch_broken",
                {"watch_id": watch_id, "reason": result["reason"]},
            )
        return result
    finally:
        conn.close()


@router.get("/monitor/events")
def list_events(watch_id: str | None = None,
                limit: int = Query(default=50, ge=1, le=200),
                _token: str = Depends(require_token)):
    conn = _conn()
    try:
        return {"events": watch_mod.list_events(conn, watch_id, limit)}
    finally:
        conn.close()


@router.post("/monitor/briefing")
def build_briefing(body: BriefingIn | None = None,
                   _token: str = Depends(require_token)):
    conn = _conn()
    try:
        force = body.force if body is not None else False
        return briefing_mod.build_briefing(conn, force=force)
    finally:
        conn.close()


@router.get("/monitor/briefing/latest")
def latest_briefing(_token: str = Depends(require_token)):
    conn = _conn()
    try:
        briefing = briefing_mod.latest_briefing(conn)
        if briefing is None:
            raise HTTPException(status_code=404, detail="no briefing yet")
        return briefing
    finally:
        conn.close()
