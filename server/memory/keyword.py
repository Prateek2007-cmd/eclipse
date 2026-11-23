"""Keyword memory (Sprint 2; scaled for PR #5 missions).

Facts keyed by topic, recalled by keyword overlap. This is the
lightweight memory the plan wants from Sprint 2 so recall works
early; vector RAG lands in Sprint 6. The server is the only writer.

Scoring: a fact scores the fraction of the query's keywords it
contains (stopwords dropped). Ties break by newest first.

PR #5 scaling (W5) — same scoring, no full scan:
- recall() asks the FTS5 index (facts_fts.py) for CANDIDATES and
  scores keyword overlap in Python over those only. The candidate
  set is a superset of the old full-scan matches, so the results
  are identical to the old recall for every query; only the work
  changes. A fast path serves the common case: facts matching ALL
  query keywords score 1.0, the maximum, so the newest `limit`
  of them are the top results with no further scoring; partial
  matches are only scored when fewer than `limit` full matches
  exist. When the FTS table is absent (a database whose
  migration never ran), recall falls back to the old full scan.
- store_fact() dedups on write: the SHA-256 of the normalised
  text, scoped — an identical fact is a no-op returning the
  existing id. Same text in two scopes is two facts.
- Facts carry a `scope` (mission id, "profile", or "global").
  recall() filters by scope FIRST; scope=None searches all
  scopes, which is today's behaviour.
- get_scoped_summary() is the briefing's read path (W3): the
  newest facts in one mission's scope.

Backward compatibility: every function here works on a
pre-migration database (no fact_hash/scope columns, no FTS
table) — dedup and scoping simply do not apply until
facts_migrate.apply_facts_migrations() has run. Existing
callers (memory_api, loop._memory_facts) are unchanged.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
import time
import uuid

from . import facts_fts

STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "of", "to", "in", "on", "for", "with", "and", "or", "but", "if",
    "then", "than", "so", "as", "at", "by", "from", "it", "its", "this",
    "that", "these", "those", "i", "you", "he", "she", "we", "they",
    "my", "your", "his", "her", "our", "their", "me", "him", "us", "them",
    "what", "which", "who", "whom", "when", "where", "why", "how",
    "do", "does", "did", "can", "could", "should", "would", "will",
    "have", "has", "had", "not", "no", "yes", "am",
}

# Scope values. "global" is the default for facts that belong to
# no mission; "profile" marks facts about the user that must
# never expire (see budgets.expire_stale_facts). A mission's
# facts use the mission id as their scope.
GLOBAL_SCOPE = "global"
PROFILE_SCOPE = "profile"
# Topic of a profile fact (the second half of the never-expire
# rule — scope OR topic "profile" protects a fact).
PROFILE_TOPIC = "profile"
# Topic of a compaction summary fact (see compaction.py).
SUMMARY_TOPIC = "summary"


def keywords(text: str) -> set[str]:
    """Lowercased word tokens with stopwords dropped."""
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    return {t for t in tokens if t not in STOPWORDS and len(t) > 1}


def normalize_fact_text(text: str) -> str:
    """Dedup normalisation: lowercase, collapse every run of
    whitespace to one space, strip. Documented here because the
    dedup hash depends on it — change this and stored hashes
    stop matching new facts."""
    return re.sub(r"\s+", " ", text.lower()).strip()


def fact_hash(text: str) -> str:
    """SHA-256 of the normalised fact text (the dedup key)."""
    return hashlib.sha256(
        normalize_fact_text(text).encode("utf-8")
    ).hexdigest()


def facts_columns(conn: sqlite3.Connection) -> set[str]:
    """Column names of the facts table (drives the pre/post
    migration branches in this module)."""
    return {r[1] for r in conn.execute("PRAGMA table_info(facts)")}


def store_fact(
    conn: sqlite3.Connection,
    text: str,
    source: str = "user",
    topic: str | None = None,
    scope: str = GLOBAL_SCOPE,
) -> str:
    """Store one fact. Returns its id.

    Dedup: when the fact_hash/scope columns exist, a fact whose
    normalised text already exists in this scope is a no-op
    returning the existing id. The same text in a different
    scope is a different fact. The unique index on
    (scope, fact_hash) is the backstop for a concurrent
    check-then-insert race — the loser re-reads and returns the
    winner's id instead of failing.

    On a pre-migration database (no new columns) this is the
    old plain insert: no dedup, no scope.
    """
    cols = facts_columns(conn)
    has_scope = "scope" in cols
    has_hash = "fact_hash" in cols
    digest = fact_hash(text) if has_hash else None
    if has_hash and has_scope:
        row = conn.execute(
            "SELECT id FROM facts WHERE scope = ? AND fact_hash = ?",
            (scope, digest),
        ).fetchone()
        if row is not None:
            return row["id"]
    fact_id = f"fact-{uuid.uuid4().hex[:12]}"
    # Column names here are fixed literals from this module, never
    # caller input — the dynamic INSERT is injection-safe.
    columns = ["id", "text", "source", "topic", "created_at"]
    values: list = [fact_id, text, source, topic, int(time.time())]
    if has_scope:
        columns.append("scope")
        values.append(scope)
    if has_hash:
        columns.append("fact_hash")
        values.append(digest)
    try:
        conn.execute(
            f"INSERT INTO facts({', '.join(columns)})"
            f" VALUES ({', '.join('?' * len(values))})",
            values,
        )
    except sqlite3.IntegrityError:
        if has_hash and has_scope:
            # Lost the dedup race — the winner's row is there now.
            row = conn.execute(
                "SELECT id FROM facts WHERE scope = ? AND fact_hash = ?",
                (scope, digest),
            ).fetchone()
            if row is not None:
                return row["id"]
        raise
    conn.commit()
    return fact_id


def delete_fact(conn: sqlite3.Connection, fact_id: str) -> bool:
    """Forget one fact. Returns False when no such fact exists.
    A fact and a RAG chunk are separate rows — this removes the
    fact only; a chunk with the same id (if one was indexed)
    is forgotten via DELETE /rag/{chunk_id}. The FTS delete
    trigger keeps the index in sync."""
    cur = conn.execute("DELETE FROM facts WHERE id = ?", (fact_id,))
    conn.commit()
    return cur.rowcount > 0


def _fts_candidate_rows(
    conn: sqlite3.Connection, query_keys: set[str], scope: str | None
) -> list:
    """Candidate rows from the FTS5 index (scope filtered first)."""
    ids = facts_fts.candidate_ids(conn, query_keys, scope=scope)
    if not ids:
        return []
    placeholders = ", ".join("?" * len(ids))
    return conn.execute(
        "SELECT * FROM facts WHERE id IN (" + placeholders + ")",
        ids,
    ).fetchall()


def _full_scan_rows(conn: sqlite3.Connection, scope: str | None) -> list:
    """The old full-scan candidate set (fallback when the FTS
    table is absent). Scope still filters, in Python."""
    rows = conn.execute("SELECT * FROM facts").fetchall()
    if scope is None or not rows:
        return rows
    if "scope" not in rows[0].keys():
        return []  # no scope column: no fact can be in this scope
    return [row for row in rows if row["scope"] == scope]


def _fact_dict(row, score: float, has_scope: bool) -> dict:
    return {
        "id": row["id"],
        "text": row["text"],
        "source": row["source"],
        "topic": row["topic"],
        "scope": row["scope"] if has_scope else None,
        "score": round(score, 3),
    }


def _score_rows(rows, query_keys: set[str], limit: int) -> list[dict]:
    """Score candidate rows by keyword overlap; return the top
    `limit` by (score, created_at), newest first. This is the
    old recall's scoring, unchanged — text and topic are
    tokenised in one pass (concatenated with a space, which
    cannot merge tokens across the boundary)."""
    has_scope = bool(rows) and "scope" in rows[0].keys()
    scored: list[tuple[float, int, dict]] = []
    for row in rows:
        fact_keys = keywords(row["text"] + " " + (row["topic"] or ""))
        overlap = len(query_keys & fact_keys)
        if overlap == 0:
            continue
        score = overlap / len(query_keys)
        scored.append(
            (score, row["created_at"],
             _fact_dict(row, score, has_scope))
        )
    scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
    return [s[2] for s in scored[:limit]]


def recall(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 5,
    scope: str | None = None,
) -> list[dict]:
    """Recall facts by keyword overlap with the query.

    Scoring is exactly the old semantics: a fact scores the
    fraction of the query's keywords it contains (in text or
    topic), ties break newest first. Only the candidate set
    changed — FTS5 MATCH instead of a full table scan.

    Fast path: facts matching ALL keywords score 1.0, the
    maximum, so when the newest `limit` of them verify at 1.0
    in Python they ARE the top results — no partial match can
    displace them and the OR candidate set is never scored.
    When fewer than `limit` full matches exist, recall falls
    back to scoring every OR candidate, so partial matches
    fill the results exactly as the old recall did.

    scope filters FIRST: a mission passes its id and searches
    only its own facts. scope=None (the default) searches all
    scopes, which is today's behaviour.
    """
    query_keys = keywords(query)
    if not query_keys or limit <= 0:
        return []
    if facts_fts.fts_exists(conn):
        # Newest full matches first; a batch of limit*10 leaves
        # room for prefix false positives ("roles" for "role")
        # that verify below 1.0 in Python.
        batch = max(limit * 10, 50)
        rows = facts_fts.full_match_rows(
            conn, query_keys, scope=scope, limit=batch
        )
        has_scope = bool(rows) and "scope" in rows[0].keys()
        perfect: list[dict] = []
        for row in rows:
            fact_keys = keywords(row["text"] + " " + (row["topic"] or ""))
            if query_keys <= fact_keys:
                perfect.append(_fact_dict(row, 1.0, has_scope))
                if len(perfect) == limit:
                    return perfect
        rows = _fts_candidate_rows(conn, query_keys, scope)
    else:
        rows = _full_scan_rows(conn, scope)
    return _score_rows(rows, query_keys, limit)


def get_scoped_summary(
    conn: sqlite3.Connection, mission_id: str, limit: int = 5
) -> list[dict]:
    """Newest facts in one mission's scope, for the briefing (W3).

    Summaries are included — after compaction they ARE the
    mission's memory. Returns [] when the scope column does not
    exist yet (pre-migration: no mission facts can exist).
    Each dict: id, text, source, topic, created_at (seconds).
    """
    if "scope" not in facts_columns(conn):
        return []
    rows = conn.execute(
        "SELECT id, text, source, topic, created_at FROM facts"
        " WHERE scope = ? ORDER BY created_at DESC, rowid DESC LIMIT ?",
        (mission_id, limit),
    ).fetchall()
    return [
        {
            "id": row["id"],
            "text": row["text"],
            "source": row["source"],
            "topic": row["topic"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]
