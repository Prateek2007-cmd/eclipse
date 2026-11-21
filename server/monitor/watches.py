"""Watches: create, check, mute, delete (Sprint 8, plan §8.1–8.3).

A watch row: id, url, selector, condition, interval_min,
last_value, misses, status (active | muted | broken),
muted_until, last_checked, last_fired_at, created_at.

Conditions:
- "changed" — the value differs from last_value.
- "appeared" — the selector was absent, now present.
- "disappeared" — the selector was present, now absent.
- "contains:<text>" — the value contains the text.

`check_watch` takes the fresh observation ({found, value}) and
returns the outcome. It never raises for a bad observation —
a missing selector is a miss, and 3 misses in a row break the
watch.
"""
from __future__ import annotations

import threading
import time
import uuid

from ..planner.scan import redact as _redact_text

MIN_INTERVAL_MIN = 15
BROKEN_AFTER_MISSES = 3
COOLDOWN_S = 3600
RETENTION_S = 30 * 24 * 3600
MAX_BRIEFINGS = 30

# check_watch is read-modify-write across several statements;
# serialise it so two concurrent checks on one watch cannot
# both read the same last_value and double-fire (M7).
_CHECK_LOCK = threading.Lock()

CONDITIONS = ("changed", "appeared", "disappeared")


def _now() -> int:
    return int(time.time())


def _preview(value: str | None) -> str | None:
    """A short redacted preview for events, reasons and briefing
    cards (M11). The full value lives only in watches.last_value,
    which the WatchCard needs; everything else that leaves this
    module carries the redacted form, the Sprint 6 precedent
    (egress audit stores hit types only)."""
    if value is None:
        return None
    redacted, _ = _redact_text(value)
    return redacted[:200]


def create_watch(conn, url: str, selector: str, condition: str,
                 interval_min: int = 60, text: str | None = None) -> dict:
    """Create a watch. Interval minimum 15 minutes (§8.1)."""
    url = url.strip()
    selector = selector.strip()
    if interval_min < MIN_INTERVAL_MIN:
        raise ValueError(
            f"interval must be at least {MIN_INTERVAL_MIN} minutes"
        )
    condition = condition.strip()
    if condition.startswith("contains:"):
        text = condition[len("contains:"):].strip()
        if not text:
            raise ValueError("contains: needs the text to look for")
        condition = "contains"
    elif condition not in CONDITIONS:
        raise ValueError(
            f"unknown condition {condition!r} — use changed, appeared,"
            " disappeared, or contains:<text>"
        )
    watch_id = f"w_{uuid.uuid4().hex[:12]}"
    now = _now()
    conn.execute(
        "INSERT INTO watches (id, url, selector, condition, interval_min,"
        " last_value, misses, status, muted_until, last_checked,"
        " last_fired_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (watch_id, url, selector, condition, interval_min, None, 0,
         "active", None, None, None, now),
    )
    if text is not None:
        conn.execute(
            "UPDATE watches SET threshold = ? WHERE id = ?",
            (text, watch_id),
        )
    conn.commit()
    return get_watch(conn, watch_id)


def get_watch(conn, watch_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM watches WHERE id = ?", (watch_id,)
    ).fetchone()
    return dict(row) if row is not None else None


def list_watches(conn, status: str | None = None) -> list[dict]:
    if status:
        rows = conn.execute(
            "SELECT * FROM watches WHERE status = ? ORDER BY created_at ASC",
            (status,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM watches ORDER BY created_at ASC"
        ).fetchall()
    return [dict(r) for r in rows]


def due_watches(conn, now: int | None = None) -> list[dict]:
    """Watches due for a check: active, or muted with the mute
    expired (B2 — a mute is a pause, not a permanent stop), and
    either never checked or past their interval."""
    now = now if now is not None else _now()
    rows = conn.execute(
        "SELECT * FROM watches"
        " WHERE (status = 'active'"
        " OR (status = 'muted' AND muted_until IS NOT NULL"
        " AND muted_until <= ?))"
        " AND (last_checked IS NULL OR last_checked + interval_min * 60 <= ?)"
        " ORDER BY created_at ASC",
        (now, now),
    ).fetchall()
    return [dict(r) for r in rows]


def repair_watch(conn, watch_id: str, selector: str | None = None,
                 url: str | None = None) -> dict | None:
    """Re-pick the element a broken watch follows (M3, §8.3).
    The watch restarts from a fresh baseline: status active,
    misses 0, last_value cleared."""
    watch = get_watch(conn, watch_id)
    if watch is None:
        return None
    new_selector = (selector or watch["selector"]).strip()
    new_url = (url or watch["url"]).strip()
    if not new_selector or not new_url:
        raise ValueError("selector and url must be non-empty")
    conn.execute(
        "UPDATE watches SET selector = ?, url = ?, status = 'active',"
        " misses = 0, last_value = NULL, last_fired_at = NULL,"
        " last_checked = NULL, muted_until = NULL WHERE id = ?",
        (new_selector, new_url, watch_id),
    )
    conn.commit()
    return get_watch(conn, watch_id)


def mute_watch(conn, watch_id: str, minutes: int = 60) -> dict | None:
    """Mute for `minutes` (one click, reversible via unmute).
    A broken watch stays broken — muting must not hide it (N3)."""
    watch = get_watch(conn, watch_id)
    if watch is None:
        return None
    until = _now() + max(1, minutes) * 60
    if watch["status"] == "active":
        conn.execute(
            "UPDATE watches SET status = 'muted', muted_until = ? WHERE id = ?",
            (until, watch_id),
        )
    else:
        conn.execute(
            "UPDATE watches SET muted_until = ? WHERE id = ?",
            (until, watch_id),
        )
    conn.commit()
    return get_watch(conn, watch_id)


def unmute_watch(conn, watch_id: str) -> dict | None:
    """Unmute: back to active with a fresh miss count, so the
    next single miss does not re-break the watch (N3)."""
    watch = get_watch(conn, watch_id)
    if watch is None:
        return None
    if watch["status"] != "muted":
        return watch
    conn.execute(
        "UPDATE watches SET status = 'active', muted_until = NULL,"
        " misses = 0 WHERE id = ?",
        (watch_id,),
    )
    conn.commit()
    return get_watch(conn, watch_id)


def delete_watch(conn, watch_id: str) -> bool:
    cur = conn.execute("DELETE FROM watches WHERE id = ?", (watch_id,))
    conn.execute("DELETE FROM watch_events WHERE watch_id = ?", (watch_id,))
    conn.commit()
    return cur.rowcount == 1


def _condition_fired(condition: str, text: str | None,
                     found: bool, had_value: bool,
                     last_value: str | None, value: str | None) -> bool:
    if condition == "changed":
        return found and had_value and value != last_value
    if condition == "appeared":
        # An empty element is not an appearance (N5).
        return found and not had_value and bool(value)
    if condition == "disappeared":
        return (not found) and had_value
    if condition == "contains":
        return found and value is not None and text is not None and text in value
    return False


def check_watch(conn, watch_id: str, found: bool,
                value: str | None) -> dict:
    """Evaluate one fresh observation for a watch.

    Returns {watch_id, fired, broken, became_broken, status,
    reason}. A firing is recorded in watch_events and the audit
    log (by the caller — this function returns the facts, it
    does not write audit entries). `became_broken` is True only
    on the transition to broken, so the API appends one audit
    entry per break, not one per repeat miss (M5).
    """
    with _CHECK_LOCK:
        return _check_watch_locked(conn, watch_id, found, value)


def _check_watch_locked(conn, watch_id: str, found: bool,
                        value: str | None) -> dict:
    watch = get_watch(conn, watch_id)
    if watch is None:
        raise ValueError(f"no watch {watch_id}")
    now = _now()
    condition = watch["condition"]
    text = watch.get("threshold")
    last_value = watch.get("last_value")
    had_value = last_value is not None
    misses = watch.get("misses") or 0
    status = watch["status"]

    # M8: a muted watch does not fire — mute is enforced by the
    # check itself, not only by due_watches.
    if status == "muted":
        return {
            "watch_id": watch_id,
            "fired": False,
            "broken": False,
            "became_broken": False,
            "status": "muted",
            "reason": "muted",
        }

    if not found:
        # "disappeared" is the one condition where a missing
        # selector IS the event, not a health problem: the thing
        # was there, now it is gone.
        if condition == "disappeared" and had_value:
            conn.execute(
                "UPDATE watches SET misses = 0, last_checked = ?,"
                " last_value = NULL, last_fired_at = ? WHERE id = ?",
                (now, now, watch_id),
            )
            conn.commit()
            _record_event(
                conn, watch_id, "fired", last_value, None,
                f"disappeared: {_preview(last_value)} -> None"
                f" on {watch['url']}",
            )
            return {
                "watch_id": watch_id,
                "fired": True,
                "broken": False,
                "became_broken": False,
                "status": status,
                "reason": f"disappeared: {_preview(last_value)} -> None",
            }
        # M2: for appeared/disappeared watches that already
        # fired, absence is the steady state — the watch did its
        # job, so a missing selector is not a health problem and
        # must not break it after the fact.
        if watch.get("last_fired_at") is not None and condition in (
            "appeared", "disappeared",
        ):
            conn.execute(
                "UPDATE watches SET last_checked = ? WHERE id = ?",
                (now, watch_id),
            )
            conn.commit()
            return {
                "watch_id": watch_id,
                "fired": False,
                "broken": False,
                "became_broken": False,
                "status": status,
                "reason": "element absent after the watch already fired",
            }
        misses += 1
        broken = misses >= BROKEN_AFTER_MISSES
        became_broken = broken and status != "broken"
        conn.execute(
            "UPDATE watches SET misses = ?, last_checked = ?,"
            " status = ? WHERE id = ?",
            (misses, now, "broken" if broken else status, watch_id),
        )
        conn.commit()
        if became_broken:
            _record_event(
                conn, watch_id, "broken",
                last_value, None,
                f"selector missing {misses} checks in a row",
            )
        return {
            "watch_id": watch_id,
            "fired": False,
            "broken": broken,
            "became_broken": became_broken,
            "status": "broken" if broken else status,
            "reason": f"selector not found ({misses}/{BROKEN_AFTER_MISSES} misses)",
        }

    # Found: reset the miss counter. A broken watch whose
    # element is back recovers to active (B6) — a broken watch
    # never silently dies, and it can heal too.
    recovered = status == "broken"
    new_status = "active" if recovered else status
    conn.execute(
        "UPDATE watches SET misses = 0, last_checked = ?, status = ?"
        " WHERE id = ?",
        (now, new_status, watch_id),
    )
    # First sighting: for "changed", "contains" and "disappeared"
    # it is the baseline — a watch must not alert on the value it
    # first saw, and "disappeared" needs a value to disappear
    # from. For "appeared" the first sighting IS the event.
    if not had_value and condition in ("changed", "contains", "disappeared"):
        conn.execute(
            "UPDATE watches SET last_value = ? WHERE id = ?", (value, watch_id)
        )
        conn.commit()
        return {
            "watch_id": watch_id,
            "fired": False,
            "broken": False,
            "became_broken": False,
            "status": new_status,
            "reason": "baseline recorded"
            + (" (recovered from broken)" if recovered else ""),
        }
    fired = _condition_fired(
        condition, text, found, had_value, last_value, value
    )
    if not fired:
        conn.commit()
        return {
            "watch_id": watch_id,
            "fired": False,
            "broken": False,
            "became_broken": False,
            "status": new_status,
            "reason": "no change"
            + (" (recovered from broken)" if recovered else ""),
        }

    # Fired — but the 1-hour cooldown dedups the SAME change
    # (§8.3: "unless the value changes again"). Suppress only
    # when the value equals the last one; a new value fires even
    # inside the hour (B1). This applies to every condition
    # (M18): a flapping appeared/disappeared element must not
    # alert on every check.
    last_fired = watch.get("last_fired_at")
    if (
        last_fired is not None
        and now - last_fired < COOLDOWN_S
        and value == last_value
    ):
        conn.commit()
        return {
            "watch_id": watch_id,
            "fired": False,
            "broken": False,
            "became_broken": False,
            "status": new_status,
            "reason": "same change, inside the 1-hour cooldown",
        }
    conn.execute(
        "UPDATE watches SET last_value = ?, last_fired_at = ? WHERE id = ?",
        (value, now, watch_id),
    )
    conn.commit()
    _record_event(
        conn, watch_id, "fired", last_value, value,
        f"{condition}: {_preview(last_value)} -> {_preview(value)}"
        f" on {watch['url']}",
    )
    return {
        "watch_id": watch_id,
        "fired": True,
        "broken": False,
        "became_broken": False,
        "status": new_status,
        "reason": f"{condition}: {_preview(last_value)} -> {_preview(value)}",
    }


def _record_event(conn, watch_id: str, kind: str,
                  old_value: str | None, new_value: str | None,
                  detail: str) -> str:
    event_id = f"we_{uuid.uuid4().hex[:12]}"
    conn.execute(
        "INSERT INTO watch_events (id, watch_id, kind, old_value,"
        " new_value, detail, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (event_id, watch_id, kind, _preview(old_value),
         _preview(new_value), _preview(detail), _now()),
    )
    # Retention (M6): events older than 30 days are pruned.
    conn.execute(
        "DELETE FROM watch_events WHERE created_at < ?",
        (_now() - RETENTION_S,),
    )
    conn.commit()
    return event_id


def list_events(conn, watch_id: str | None = None,
                limit: int = 50, since: int | None = None) -> list[dict]:
    limit = max(1, min(int(limit), 200))
    clauses = []
    params: list = []
    if watch_id:
        clauses.append("watch_id = ?")
        params.append(watch_id)
    if since is not None:
        clauses.append("created_at >= ?")
        params.append(since)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    rows = conn.execute(
        f"SELECT * FROM watch_events {where}"
        " ORDER BY created_at DESC, rowid DESC LIMIT ?",
        params,
    ).fetchall()
    return [dict(r) for r in rows]
