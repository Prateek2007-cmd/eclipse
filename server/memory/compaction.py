"""Mission-fact compaction (PR #5, W5).

The real scaling trick: when a mission's raw facts reach a
threshold (default 500), the oldest batch is merged into ONE
summary fact and the raw rows are deleted. Summaries are
themselves facts (topic "summary", source "compaction"), so
nothing is silently lost — the history survives compressed.

The summary is built with rules/regexes over the raw fact
text — deterministic, no planner call, so compaction works
offline with no keys. Key numbers are preserved:
- price facts: the min/max range + check count + time span;
- role facts: counts by outcome (matched / applied / skipped /
  ineligible / ...);
- anything else: count + time span + most frequent keywords.

Insert-before-delete: the summary is stored FIRST, then the
raw rows are deleted. If the delete fails, the raw facts are
still there (duplication, not loss). The newest `keep_recent`
raw facts are never compacted, so recent detail survives for
the briefing.
"""
from __future__ import annotations

import re
import sqlite3
from collections import Counter
from datetime import datetime, timezone

from .keyword import (
    SUMMARY_TOPIC,
    facts_columns,
    keywords,
    store_fact,
)

DEFAULT_THRESHOLD = 500
DEFAULT_KEEP_RECENT = 100

# ₹1,299 / Rs. 1299 / INR 1299 — the price shapes the runner writes.
_PRICE_RE = re.compile(r"(?:₹|Rs\.?|INR)\s*([\d,]+(?:\.\d{1,2})?)")

# Role/mail outcomes, counted per fact. A fact's outcome is the
# leftmost outcome keyword in its text (word-boundary matched, so
# "ineligible" does not also match "eligible").
OUTCOMES = (
    "applied", "matched", "skipped", "ineligible", "drafted",
    "sent", "rejected", "eligible",
)
_OUTCOME_RES = {
    outcome: re.compile(r"\b" + re.escape(outcome) + r"\b")
    for outcome in OUTCOMES
}


def _extract_prices(text: str) -> list[float]:
    return [float(m.replace(",", "")) for m in _PRICE_RE.findall(text)]


def _fact_outcome(text: str) -> str | None:
    """The leftmost outcome keyword in the text, or None."""
    best: tuple[int, str] | None = None
    lowered = text.lower()
    for outcome, pattern in _OUTCOME_RES.items():
        match = pattern.search(lowered)
        if match is not None and (best is None or match.start() < best[0]):
            best = (match.start(), outcome)
    return best[1] if best is not None else None


def _fmt_inr(value: float) -> str:
    if value == int(value):
        return f"\u20b9{value:,.0f}"
    return f"\u20b9{value:,.2f}"


def _humanize_span(seconds: int) -> str:
    """14 days of seconds -> '2 weeks' (summary-friendly).
    Rounded to the nearest unit — a 13.97-day span reads as
    '2 weeks', not '1 week'."""
    if seconds < 60:
        return "less than a minute"
    if seconds < 3600:
        n = max(1, round(seconds / 60))
        return f"{n} minute{'s' if n != 1 else ''}"
    if seconds < 86400:
        n = max(1, round(seconds / 3600))
        return f"{n} hour{'s' if n != 1 else ''}"
    if seconds < 7 * 86400:
        n = max(1, round(seconds / 86400))
        return f"{n} day{'s' if n != 1 else ''}"
    if seconds < 30 * 86400:
        n = max(1, round(seconds / (7 * 86400)))
        return f"{n} week{'s' if n != 1 else ''}"
    n = max(1, round(seconds / (30 * 86400)))
    return f"{n} month{'s' if n != 1 else ''}"


def _date_range(oldest: int, newest: int) -> str:
    """UTC date range, e.g. '(2026-11-09 → 2026-11-23)'."""
    first = datetime.fromtimestamp(oldest, timezone.utc).date().isoformat()
    last = datetime.fromtimestamp(newest, timezone.utc).date().isoformat()
    if first == last:
        return f"({first})"
    return f"({first} \u2192 {last})"


def _dominant_topic(rows: list) -> str | None:
    """The topic shared by >= 80% of the batch, if there is one."""
    topics = [row["topic"] for row in rows if row["topic"]]
    if not topics:
        return None
    topic, count = Counter(topics).most_common(1)[0]
    return topic if count * 5 >= len(rows) * 4 else None


def _top_keywords(texts: list[str], n: int = 5) -> list[str]:
    """Most frequent keywords across the batch (numbers dropped —
    they are noise in a generic summary). Ties break
    alphabetically so the summary is deterministic run to run
    (Counter.most_common breaks ties by insertion order, which
    follows set iteration — randomised per process)."""
    counter: Counter = Counter()
    for text in texts:
        counter.update(t for t in keywords(text) if not t.isdigit())
    ranked = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
    return [word for word, _ in ranked[:n]]


# Topics that name the category itself ("Price checks for
# price" reads wrong) — the summary label only names a
# distinguishing topic, e.g. "Price checks for headphones".
_GENERIC_TOPICS = {"price", "role", "check", "summary"}


def build_summary_text(rows: list) -> str:
    """One summary sentence for a batch of raw facts.

    Classification is by majority: price when >= half the batch
    carries a ₹ amount, role when >= half carries an outcome
    keyword, generic otherwise. A mixed batch gets the majority
    sentence plus a count of the rest.
    """
    if not rows:
        return "[summary] No observations."
    oldest = min(row["created_at"] for row in rows)
    newest = max(row["created_at"] for row in rows)
    span = _humanize_span(max(0, newest - oldest))
    dates = _date_range(oldest, newest)
    topic = _dominant_topic(rows)
    label = (
        f" for {topic}"
        if topic and topic not in _GENERIC_TOPICS
        else ""
    )

    price_rows = [row for row in rows if _extract_prices(row["text"])]
    if len(price_rows) * 2 >= len(rows):
        prices = [
            price for row in price_rows for price in _extract_prices(row["text"])
        ]
        text = (
            f"[summary] Price checks{label} ({len(price_rows)} checks): "
            f"{_fmt_inr(min(prices))}\u2013{_fmt_inr(max(prices))}"
            f" over {span} {dates}."
        )
        rest = len(rows) - len(price_rows)
        if rest:
            text += f" Plus {rest} other observations."
        return text

    outcomes = [
        outcome
        for row in rows
        if (outcome := _fact_outcome(row["text"])) is not None
    ]
    if len(outcomes) * 2 >= len(rows):
        counts = Counter(outcomes)
        parts = [f"{n} {o}" for o, n in counts.most_common()]
        text = (
            f"[summary] Roles seen{label} ({len(outcomes)}): "
            f"{', '.join(parts)}. Over {span} {dates}."
        )
        rest = len(rows) - len(outcomes)
        if rest:
            text += f" Plus {rest} other observations."
        return text

    text = f"[summary] {len(rows)} observations over {span} {dates}."
    top = _top_keywords([row["text"] for row in rows])
    if top:
        text += f" Frequent terms: {', '.join(top)}."
    return text


def _count_in_scope(conn: sqlite3.Connection, mission_id: str,
                    raw_only: bool) -> int:
    if "scope" not in facts_columns(conn):
        return 0
    sql = "SELECT COUNT(*) AS c FROM facts WHERE scope = ?"
    if raw_only:
        sql += " AND (topic IS NULL OR topic <> 'summary')"
    row = conn.execute(sql, (mission_id,)).fetchone()
    return int(row["c"])


def count_facts_in_scope(conn: sqlite3.Connection, mission_id: str) -> int:
    """All facts in a mission's scope (raw + summaries)."""
    return _count_in_scope(conn, mission_id, raw_only=False)


def count_raw_in_scope(conn: sqlite3.Connection, mission_id: str) -> int:
    """Raw (non-summary) facts in a mission's scope."""
    return _count_in_scope(conn, mission_id, raw_only=True)


def _fetch_oldest(conn: sqlite3.Connection, mission_id: str, n: int,
                  include_summaries: bool) -> list:
    sql = (
        "SELECT id, text, topic, created_at FROM facts WHERE scope = ?"
    )
    if not include_summaries:
        sql += " AND (topic IS NULL OR topic <> 'summary')"
    sql += " ORDER BY created_at ASC, rowid ASC LIMIT ?"
    return conn.execute(sql, (mission_id, n)).fetchall()


def compact_oldest(
    conn: sqlite3.Connection,
    mission_id: str,
    n: int,
    include_summaries: bool = False,
) -> dict:
    """Merge the n oldest facts in a scope into one summary fact.

    The primitive behind compact_mission_facts and the budget
    enforcer. Summaries are excluded by default; the budget
    enforcer passes include_summaries=True when raw facts run
    out (a summary of summaries — lossy, but bounded, and still
    better than deleting un-summarised).
    Returns {"mission_id", "compacted", "summary_id", "summary_text"}.
    """
    result = {
        "mission_id": mission_id,
        "compacted": 0,
        "summary_id": None,
        "summary_text": None,
    }
    if n <= 0 or "scope" not in facts_columns(conn):
        return result
    batch = _fetch_oldest(conn, mission_id, n, include_summaries)
    if not batch:
        return result
    text = build_summary_text(batch)
    # Summary FIRST, delete second — a failure between the two
    # leaves duplication, never loss.
    summary_id = store_fact(
        conn, text, source="compaction", topic=SUMMARY_TOPIC,
        scope=mission_id,
    )
    conn.executemany(
        "DELETE FROM facts WHERE id = ?",
        [(row["id"],) for row in batch],
    )
    conn.commit()
    result.update(
        compacted=len(batch), summary_id=summary_id, summary_text=text
    )
    return result


def compact_mission_facts(
    conn: sqlite3.Connection,
    mission_id: str,
    threshold: int = DEFAULT_THRESHOLD,
    keep_recent: int = DEFAULT_KEEP_RECENT,
) -> dict:
    """Compact a mission's facts when its raw count reaches the
    threshold: the oldest raw facts (all but the newest
    `keep_recent`) merge into one summary fact and the raw rows
    are deleted. Returns counts + the summary text.

    No-op (triggered=False) when the raw count is below the
    threshold or when keep_recent already covers every raw fact.
    """
    raw_before = count_raw_in_scope(conn, mission_id)
    result = {
        "mission_id": mission_id,
        "threshold": threshold,
        "keep_recent": keep_recent,
        "raw_before": raw_before,
        "compacted": 0,
        "raw_after": raw_before,
        "summary_id": None,
        "summary_text": None,
        "triggered": False,
    }
    if raw_before < threshold:
        return result
    n = raw_before - keep_recent
    if n <= 0:
        return result
    part = compact_oldest(conn, mission_id, n)
    if part["compacted"] == 0:
        return result
    result.update(
        compacted=part["compacted"],
        raw_after=raw_before - part["compacted"],
        summary_id=part["summary_id"],
        summary_text=part["summary_text"],
        triggered=True,
    )
    return result
