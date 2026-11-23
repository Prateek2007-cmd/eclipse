"""Per-mission fact budgets + expiry (PR #5, W5).

Budgets are enforced server-side, like the action bounds
(W1's bounds.py): the mission runner calls enforce_fact_budget
after a run that wrote facts, and the count comes back under
the cap without ever deleting un-summarised facts.

Expiry deletes stale OBSERVATION facts. The never-expire rule
is defined here and in keyword.py:
- scope == "profile" OR topic == "profile": a profile fact —
  NEVER expired (the user's own details must not age out);
- topic == "summary": a compaction summary — NEVER expired
  (it is the only record of the raw facts it replaced;
  expiring it would silently destroy history).
Everything else with created_at older than the TTL is an
observation and may go. Rows with a NULL created_at are never
expired (unknown age — fail safe).
"""
from __future__ import annotations

import sqlite3
import time

from .compaction import (
    compact_oldest,
    count_facts_in_scope,
    count_raw_in_scope,
)
from .keyword import PROFILE_SCOPE, PROFILE_TOPIC, SUMMARY_TOPIC, facts_columns

# The budget loop converges in one round for any realistic
# overrun (each round removes the whole excess); this bound is
# only a safety net against a logic slip looping forever.
_MAX_BUDGET_ROUNDS = 10


def enforce_fact_budget(
    conn: sqlite3.Connection, mission_id: str, max_facts: int
) -> dict:
    """Bring a mission's fact count down to max_facts by
    compacting the oldest facts (never deleting un-summarised).

    One compaction of (excess + 1) raw facts into one summary
    removes exactly `excess` facts, so one round normally
    suffices. When raw facts run out, the oldest summaries are
    merged into a meta-summary instead. The last remaining fact
    is never compacted away (compacting 1 fact into 1 summary
    removes nothing).
    Returns {"mission_id", "max_facts", "before", "after",
    "compacted", "summaries_created", "rounds"}.
    """
    if max_facts < 1:
        raise ValueError("max_facts must be >= 1")
    before = count_facts_in_scope(conn, mission_id)
    result = {
        "mission_id": mission_id,
        "max_facts": max_facts,
        "before": before,
        "after": before,
        "compacted": 0,
        "summaries_created": 0,
        "rounds": 0,
    }
    if before <= max_facts:
        return result
    for _ in range(_MAX_BUDGET_ROUNDS):
        total = count_facts_in_scope(conn, mission_id)
        if total <= max_facts:
            break
        raw = count_raw_in_scope(conn, mission_id)
        excess = total - max_facts
        if raw >= 2:
            # Compact raw facts only; n = excess + 1 removes
            # exactly `excess` net facts (n raw -> 1 summary).
            part = compact_oldest(
                conn, mission_id, min(raw, excess + 1)
            )
        elif total >= 2:
            # Raw facts are (nearly) gone: merge the oldest
            # facts — raw + summaries — into one meta-summary,
            # keeping at least one fact standing.
            n = min(total - 1, excess + 1)
            if n < 2:
                break
            part = compact_oldest(
                conn, mission_id, n, include_summaries=True
            )
        else:
            break
        if part["compacted"] == 0:
            break
        result["compacted"] += part["compacted"]
        result["summaries_created"] += 1
        result["rounds"] += 1
    result["after"] = count_facts_in_scope(conn, mission_id)
    return result


def expire_stale_facts(
    conn: sqlite3.Connection,
    mission_id: str | None = None,
    ttl_days: int = 90,
) -> int:
    """Delete observation facts older than ttl_days. Returns the
    number deleted.

    mission_id scopes the expiry to one mission; None expires
    across all scopes. Profile facts (scope or topic "profile")
    and summaries (topic "summary") are NEVER expired — see the
    module docstring for the rule. The FTS delete trigger keeps
    the index in sync with the deletes.
    """
    cutoff = int(time.time()) - int(ttl_days * 86400)
    cols = facts_columns(conn)
    if mission_id is not None and "scope" not in cols:
        return 0  # no scope column: no mission facts can exist
    conditions = ["created_at IS NOT NULL", "created_at < ?"]
    params: list = [cutoff]
    if "scope" in cols:
        conditions.append("(scope IS NULL OR scope <> ?)")
        params.append(PROFILE_SCOPE)
        if mission_id is not None:
            conditions.append("scope = ?")
            params.append(mission_id)
    conditions.append("(topic IS NULL OR topic NOT IN (?, ?))")
    params.extend([PROFILE_TOPIC, SUMMARY_TOPIC])
    cur = conn.execute(
        "DELETE FROM facts WHERE " + " AND ".join(conditions), params
    )
    conn.commit()
    return cur.rowcount
