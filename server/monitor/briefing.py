"""Morning briefing (Sprint 8, plan §8.4; PR #5: mission cards
+ IST day guard).

Up to 10 BriefingCards: watch firings since the last briefing,
mission news (applied / drafts waiting / approvals needed /
waiting for Chrome / stalled), plus one inbox scan (Gmail
connector, read-only) — and at least one card even on a quiet
morning, never a silent empty briefing. Generated on demand
(the user opens the workspace), once each morning at 09:00 IST
by the extension alarm, and by the server mission tick (the
backstop when Chrome is closed). Each card has one action button.

A briefing never sends mail or messages by itself. It shows;
the user acts.

Day guard: at most one briefing is built per IST calendar day
unless force=True. Both triggers (server tick + extension
alarm) call the same endpoint, so without the guard the user
would get two briefings on the same morning. All IST math uses
zoneinfo.ZoneInfo("Asia/Kolkata") — never the host localtime,
whose timezone is not guaranteed.
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

MAX_CARDS = 10
SINCE_S = 24 * 3600

IST = ZoneInfo("Asia/Kolkata")
BRIEFING_HOUR_IST = 9
BRIEFING_DAY_KEY = "briefing:last_day_ist"
WORKSPACE_URL = "http://127.0.0.1:8000/workspace"


def ist_date(epoch_s: int) -> str:
    """IST calendar date (YYYY-MM-DD) for an epoch second."""
    return datetime.fromtimestamp(epoch_s, tz=IST).strftime("%Y-%m-%d")


def today_briefing_ist(now_s: int) -> int:
    """Epoch seconds of today's 09:00 IST (the briefing time)."""
    now_ist = datetime.fromtimestamp(now_s, tz=IST)
    today = now_ist.replace(hour=BRIEFING_HOUR_IST, minute=0, second=0,
                            microsecond=0)
    return int(today.timestamp())


def next_briefing_ist(now_s: int) -> int:
    """Epoch seconds of the next 09:00 IST at or after now_s.

    The mission runner tick uses this: it builds the briefing
    once now_s passes today's 09:00 IST (the extension alarm
    is the other trigger; the day guard makes the double
    trigger harmless)."""
    today = today_briefing_ist(now_s)
    if today >= now_s:
        return today
    return int((datetime.fromtimestamp(today, tz=IST)
                + timedelta(days=1)).timestamp())


def _memory_get(conn, key: str) -> str | None:
    row = conn.execute(
        "SELECT value FROM memory WHERE key = ?", (key,)
    ).fetchone()
    return row["value"] if row is not None else None


def _memory_set(conn, key: str, value: str, now: int) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO memory (key, value, kind, updated_at)"
        " VALUES (?, ?, 'system', ?)",
        (key, value, now),
    )


def _table_exists(conn, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _has_column(conn, table: str, column: str) -> bool:
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    return column in cols


def _safe_json(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _scoped_latest(conn, mission_id: str) -> str | None:
    """Most recent scoped fact text for a mission, or None.

    W5's contract: server.memory.keyword.get_scoped_summary(conn,
    mission_id, limit=3) returns a list of recent scoped fact
    dicts with "text", newest first. Imported lazily — W5's
    module may not exist yet, in which case the card is built
    without the line. A failure here must never fail the
    briefing, so every error degrades to None.
    """
    try:
        from server.memory.keyword import get_scoped_summary
    except (ImportError, AttributeError):
        return None
    try:
        facts = get_scoped_summary(conn, mission_id, limit=3)
    except Exception:  # noqa: BLE001 - fact store is best-effort
        return None
    if not isinstance(facts, list):
        return None
    for fact in facts:
        if isinstance(fact, dict):
            text = fact.get("text")
            if isinstance(text, str) and text.strip():
                return text.strip()
    return None


def _live_mission_approvals(conn, now: int) -> list[dict]:
    """Live pending approvals across all missions, as
    {mission_id, mission_name, action} rows.

    Uses W1's links.py per the §4 contract when it exists
    (pending_approvals_for_mission per mission); falls back to
    the same join in SQL when it does not (the tables are the
    contract either way). Expired approvals are never listed.
    """
    missions = {
        row["id"]: row["name"]
        for row in conn.execute("SELECT id, name FROM missions").fetchall()
    }
    if not missions:
        return []
    try:
        from server.missions import links as links_mod
    except ImportError:
        links_mod = None
    if links_mod is not None:
        approvals: list[dict] = []
        for mission_id, name in missions.items():
            for approval in links_mod.pending_approvals_for_mission(
                conn, mission_id
            ):
                approvals.append(
                    {
                        "mission_id": mission_id,
                        "mission_name": name,
                        "action": approval.get("action", ""),
                    }
                )
        return approvals
    if not _has_column(conn, "tasks", "mission_id"):
        return []
    rows = conn.execute(
        "SELECT p.action, t.mission_id FROM pending_approvals p"
        " JOIN tasks t ON p.task_id = t.id"
        " WHERE t.mission_id IS NOT NULL AND p.expires_at > ?",
        (now,),
    ).fetchall()
    return [
        {
            "mission_id": row["mission_id"],
            "mission_name": missions.get(row["mission_id"], "a mission"),
            "action": row["action"],
        }
        for row in rows
    ]


def _mission_cards(conn, now: int, since: int, budget: int) -> list[dict]:
    """Mission news cards (§3.9), in design order, at most
    `budget` cards. Uses the existing {kind, title, body,
    action, action_label} shape, so the workspace briefing
    renderer needs no change. Returns [] when the missions
    tables do not exist yet (pre-PR #5 database)."""
    if budget <= 0:
        return []
    if not _table_exists(conn, "missions"):
        return []
    if not _table_exists(conn, "mission_activity"):
        return []
    cards: list[dict] = []

    # Applied in the last 24 h — one card per application.
    applied = conn.execute(
        "SELECT a.mission_id, a.detail, m.status FROM mission_activity a"
        " JOIN missions m ON a.mission_id = m.id"
        " WHERE a.kind = 'applied' AND a.created_at >= ?"
        " ORDER BY a.created_at DESC",
        (since,),
    ).fetchall()
    for row in applied:
        if len(cards) >= budget:
            break
        detail = _safe_json(row["detail"])
        title = detail.get("title") or "a role"
        company = detail.get("company")
        heading = (
            f"Applied to {title} at {company}" if company
            else f"Applied to {title}"
        )
        body = str(detail.get("confirmation") or detail.get("role_id") or "")[:200]
        # Scoped fact line (W5 contract): each ACTIVE mission's
        # card carries its most recent scoped fact. Aggregate
        # cards (drafts / approvals / waiting) cover several
        # missions, so they carry no line; stalled missions are
        # paused, not active, so neither do they.
        if row["status"] == "active":
            latest = _scoped_latest(conn, row["mission_id"])
            if latest is not None:
                line = f"Latest: {latest[:200]}"
                body = f"{body}\n{line}" if body else line
        cards.append(
            {
                "kind": "mission_applied",
                "title": heading,
                "body": body,
                "action": detail.get("url"),
                "action_label": "Open role" if detail.get("url") else "",
            }
        )

    # Cold-mail drafts waiting for approval — one summary card.
    if len(cards) < budget and _has_column(conn, "tasks", "mission_id"):
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM mail_drafts d"
            " JOIN tasks t ON d.task_id = t.id"
            " WHERE t.mission_id IS NOT NULL AND d.status = 'draft'"
        ).fetchone()
        n_drafts = row["n"] if row is not None else 0
        if n_drafts > 0:
            noun = "draft" if n_drafts == 1 else "drafts"
            cards.append(
                {
                    "kind": "mission_draft_ready",
                    "title": (
                        f"{n_drafts} cold-mail {noun} waiting for "
                        "your approval"
                    ),
                    "body": (
                        "A mission prepared cold mail and is waiting "
                        "for you to approve it before anything is sent."
                    ),
                    "action": WORKSPACE_URL,
                    "action_label": "Review drafts",
                }
            )

    # Live approvals a mission is blocked on — one summary card.
    if len(cards) < budget:
        approvals = _live_mission_approvals(conn, now)
        if approvals:
            names = sorted({a["mission_name"] for a in approvals})
            noun = "approval" if len(approvals) == 1 else "approvals"
            cards.append(
                {
                    "kind": "mission_approval_needed",
                    "title": (
                        f"A mission needs your approval "
                        f"({len(approvals)} {noun} waiting)"
                    ),
                    "body": (
                        "Waiting on: " + ", ".join(names) + ". Nothing "
                        "is applied or sent until you approve it."
                    ),
                    "action": WORKSPACE_URL,
                    "action_label": "Review approvals",
                }
            )

    # Missions waiting for the browser — one summary card.
    if len(cards) < budget:
        waiting = conn.execute(
            "SELECT name FROM missions WHERE status = 'active'"
            " AND last_run_status = 'waiting_for_extension'"
            " ORDER BY name"
        ).fetchall()
        if waiting:
            names = [row["name"] for row in waiting]
            cards.append(
                {
                    "kind": "mission_waiting",
                    "title": "Missions are waiting for Chrome",
                    "body": (
                        "Waiting: " + ", ".join(names) + ". Open Chrome "
                        "so the mission can continue its browser work."
                    ),
                    "action": WORKSPACE_URL,
                    "action_label": "Open workspace",
                }
            )

    # Stalled / auto-paused missions — one card each (they are
    # rare, and each names its own reason).
    if len(cards) < budget:
        stalled = conn.execute(
            "SELECT name, last_error FROM missions"
            " WHERE status = 'paused'"
            " AND last_run_status IN ('stalled', 'failed')"
            " ORDER BY name"
        ).fetchall()
        for row in stalled:
            if len(cards) >= budget:
                break
            reason = row["last_error"] or "it kept failing"
            cards.append(
                {
                    "kind": "mission_stalled",
                    "title": f"A mission stalled: {row['name']}",
                    "body": (
                        f"Paused automatically — {reason}. Resume it "
                        "from the Missions view once the cause is fixed."
                    ),
                    "action": WORKSPACE_URL,
                    "action_label": "Open workspace",
                }
            )

    return cards


def build_briefing(conn, gmail=None, force: bool = False) -> dict:
    """Build the briefing from watch firings + mission news +
    one Gmail scan.

    Returns {id, created_at, cards: [{kind, title, body, action,
    action_label}]}. The briefing is stored so the workspace can
    show the latest one without rebuilding it.

    Day guard: without force, a second build on the same IST
    calendar day returns the stored briefing instead of building
    a new one (the server tick and the extension alarm both
    trigger a build — the guard makes the double trigger
    harmless). With force=True (the workspace "Build now"
    button) it always builds.
    """
    from server.connectors import registry as conn_registry
    from server.monitor import watches as watch_mod

    now = int(time.time())
    today = ist_date(now)
    if not force and _memory_get(conn, BRIEFING_DAY_KEY) == today:
        stored = latest_briefing(conn)
        if stored is not None:
            return stored
        # The guard key is set but no briefing is stored (a
        # crash between the two writes, or a hand-edited DB) —
        # fall through and build one instead of returning nothing.
    since = now - SINCE_S
    cards: list[dict] = []

    events = watch_mod.latest_events_per_watch(conn, since=since)
    seen_watches: set[str] = set()
    for event in events:
        watch = watch_mod.get_watch(conn, event["watch_id"])
        if watch is None:
            continue
        # N7: one chatty watch must not fill all 10 cards — the
        # latest event per watch is enough for a briefing.
        if event["watch_id"] in seen_watches:
            continue
        seen_watches.add(event["watch_id"])
        if event["kind"] == "fired":
            cards.append(
                {
                    "kind": "watch",
                    "title": f"Change on {watch['url']}",
                    "body": event["detail"],
                    "action": watch["url"],
                    "action_label": "Open page",
                }
            )
        elif event["kind"] == "broken":
            # The watch may have recovered since it broke — a
            # broken card is only true while it is still broken.
            if watch["status"] != "broken":
                continue
            cards.append(
                {
                    "kind": "watch_broken",
                    "title": f"Watch broken: {watch['url']}",
                    "body": (
                        "The element this watch follows is gone. "
                        "Pick the element again to fix it."
                    ),
                    "action": watch["url"],
                    "action_label": "Open page",
                }
            )
        if len(cards) >= MAX_CARDS:
            break

    # Mission news outranks the inbox scan: it goes after the
    # watch cards and before Gmail. A failure here must not kill
    # the whole briefing — the mission tables are new, and a
    # briefing with watch + inbox cards is better than none.
    if len(cards) < MAX_CARDS:
        try:
            cards.extend(
                _mission_cards(conn, now, since, MAX_CARDS - len(cards))
            )
        except Exception:  # noqa: BLE001 - mission cards are best-effort
            pass

    # A disabled connector refuses work — the briefing must not
    # scan an inbox the user turned off. Fail closed, like the
    # API's _require_on: a missing registry row is not "on".
    gmail_reg = conn_registry.get(conn, "gmail")
    gmail_off = gmail_reg is None or not gmail_reg["enabled"]
    if len(cards) < MAX_CARDS and not gmail_off:
        try:
            from server.connectors.gmail import GmailConnector

            connector = gmail or GmailConnector()
            offers = connector.search_offers(MAX_CARDS - len(cards))
            for offer in offers:
                if len(cards) >= MAX_CARDS:
                    break
                cards.append(
                    {
                        "kind": "offer",
                        "title": offer.get("subject") or "Mail from inbox",
                        "body": (offer.get("body_redacted") or "")[:200],
                        "action": offer.get("id"),
                        "action_label": "Open mail",
                    }
                )
        except Exception:  # noqa: BLE001 - inbox scan is best-effort
            # N4: say so on a card instead of silently shipping
            # a briefing with no inbox scan.
            cards.append(
                {
                    "kind": "inbox_unavailable",
                    "title": "Inbox scan unavailable",
                    "body": (
                        "Gmail could not be reached, so this briefing "
                        "has watch firings only."
                    ),
                    "action": None,
                    "action_label": "",
                }
            )

    if not cards:
        # Never a silent empty briefing: when nothing fired and
        # no offer mail arrived, say so on a card.
        cards.append(
            {
                "kind": "quiet",
                "title": "Nothing new this morning",
                "body": (
                    "No watched page changed and no new offer "
                    "mail arrived."
                ),
                "action": None,
                "action_label": "",
            }
        )

    briefing_id = f"br_{uuid.uuid4().hex[:12]}"
    conn.execute(
        "INSERT INTO briefings (id, created_at, cards_json) VALUES (?, ?, ?)",
        (briefing_id, now, json.dumps(cards)),
    )
    # Retention (M6): keep only the latest 30 briefings.
    conn.execute(
        "DELETE FROM briefings WHERE id NOT IN ("
        " SELECT id FROM briefings ORDER BY created_at DESC LIMIT 30)",
    )
    # Day guard: remember this IST day so a second build today
    # returns the stored briefing instead of building again.
    _memory_set(conn, BRIEFING_DAY_KEY, today, now)
    conn.commit()
    return {"id": briefing_id, "created_at": now, "cards": cards}


def latest_briefing(conn) -> dict | None:
    # rowid breaks created_at ties: two briefings built in the
    # same second (e.g. a force rebuild right after a build)
    # must resolve to the one actually built last, or the day
    # guard would hand back the older briefing.
    row = conn.execute(
        "SELECT * FROM briefings"
        " ORDER BY created_at DESC, rowid DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "cards": json.loads(row["cards_json"]),
    }
