"""Morning briefing (Sprint 8, plan §8.4).

Up to 10 BriefingCards: watch firings since the last briefing
plus one inbox scan (Gmail connector, read-only) — and at least
one card even on a quiet morning, never a silent empty briefing.
Generated on
demand (the user opens the workspace) and once each morning
while Chrome is open. Each card has one action button.

A briefing never sends mail or messages by itself. It shows;
the user acts.
"""
from __future__ import annotations

import json
import time
import uuid

MAX_CARDS = 10
SINCE_S = 24 * 3600


def build_briefing(conn, gmail=None) -> dict:
    """Build the briefing from watch firings + one Gmail scan.

    Returns {id, created_at, cards: [{kind, title, body, action,
    action_label}]}. The briefing is stored so the workspace can
    show the latest one without rebuilding it.
    """
    from server.connectors import registry as conn_registry
    from server.monitor import watches as watch_mod

    now = int(time.time())
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
    conn.commit()
    return {"id": briefing_id, "created_at": now, "cards": cards}


def latest_briefing(conn) -> dict | None:
    row = conn.execute(
        "SELECT * FROM briefings ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "cards": json.loads(row["cards_json"]),
    }
