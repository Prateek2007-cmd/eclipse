"""FTS5 index over the keyword facts table (PR #5, W5).

The old recall() did a FULL table scan — SELECT with no WHERE,
then tokenised every fact in Python. Fine at 100 facts, unusable
at 100k. This module keeps a SQLite FTS5 virtual table in sync
with `facts` via triggers, so recall() asks FTS5 for CANDIDATES
and scores keyword overlap in Python over those only. The scoring
semantics (keyword overlap, newest-first tiebreak) are unchanged;
only the candidate set changes, from "every fact" to "facts
containing at least one query keyword".

Stack: SQLite + FTS5 built into Python's sqlite3. No new
database, no new dependency. FTS5 availability was verified on
this Python (SQLite 3.45.1, ENABLE_FTS5=1) before this was
written; recall() still falls back to the full scan when the
virtual table is absent (a pre-migration database), so memory
keeps working even if the migration never ran.

The FTS table is a CONTENT table (it stores the text itself, not
an external-content pointer), so the sync triggers use a plain
DELETE — the FTS5 'delete' special command is only for external
content tables and fails with "SQL logic error" on a content
table (verified 2026-11-23).

`fact_id` and `scope` are UNINDEXED columns: they are stored so
candidates can be joined back to `facts` and filtered by scope
("scope FIRST" — a mission searches its own facts), but they are
not part of the full-text index.
"""
from __future__ import annotations

import re
import sqlite3

FTS_TABLE = "facts_fts"

# Bound on the Python scoring work per recall. A query whose
# keywords match more facts than this is scored over the best
# bm25-ranked candidates only — a pathological query ("the" is a
# stopword, but "price" over 50k price facts is real) must not
# turn recall back into a full scan. 5000 is far above any
# realistic overlap set for a keyword query.
CANDIDATE_LIMIT = 5000

_CREATE_FTS_SQL = f"""
CREATE VIRTUAL TABLE IF NOT EXISTS {FTS_TABLE} USING fts5(
  text, topic, fact_id UNINDEXED, scope UNINDEXED,
  tokenize='unicode61')
"""

# Sync triggers. rowid is the join key between `facts` and the
# FTS table (a fact's rowid is stable for its lifetime); fact_id
# is the stable key callers join on.
_CREATE_TRIGGERS_SQL = f"""
CREATE TRIGGER IF NOT EXISTS facts_fts_ai AFTER INSERT ON facts BEGIN
  INSERT INTO {FTS_TABLE}(rowid, text, topic, fact_id, scope)
  VALUES (new.rowid, new.text, new.topic, new.id, new.scope);
END;
CREATE TRIGGER IF NOT EXISTS facts_fts_ad AFTER DELETE ON facts BEGIN
  DELETE FROM {FTS_TABLE} WHERE rowid = old.rowid;
END;
CREATE TRIGGER IF NOT EXISTS facts_fts_au AFTER UPDATE ON facts BEGIN
  DELETE FROM {FTS_TABLE} WHERE rowid = old.rowid;
  INSERT INTO {FTS_TABLE}(rowid, text, topic, fact_id, scope)
  VALUES (new.rowid, new.text, new.topic, new.id, new.scope);
END;
"""

# A MATCH term may only come from keywords(): lowercase alphanumerics.
# Anything else is dropped, never interpolated — user query text
# must never reach the MATCH string unescaped.
_TERM_RE = re.compile(r"^[a-z0-9]+$")


def fts_exists(conn: sqlite3.Connection) -> bool:
    """True when the FTS5 virtual table is present in this DB."""
    row = conn.execute(
        "SELECT name FROM sqlite_master"
        " WHERE type = 'table' AND name = ?",
        (FTS_TABLE,),
    ).fetchone()
    return row is not None


def ensure_fts(conn: sqlite3.Connection) -> None:
    """Create the FTS5 table + sync triggers if missing.

    Idempotent (IF NOT EXISTS throughout). Does NOT populate the
    index from existing rows — that is backfill_fts(), which the
    migration calls right after this.
    """
    conn.execute(_CREATE_FTS_SQL)
    conn.executescript(_CREATE_TRIGGERS_SQL)


def backfill_fts(conn: sqlite3.Connection) -> dict:
    """Sync the FTS index with `facts` (migration + repair).

    Inserts rows for facts missing from the index and removes
    stale index rows for facts that no longer exist (e.g. a
    trigger was missing for a while). Idempotent — safe to run
    twice; the second run inserts/removes nothing.
    Returns {"inserted": n, "removed": m}.

    The diff is computed in Python (one scan of each table):
    a SQL NOT EXISTS over the FTS table would be O(n²) because
    fact_id is UNINDEXED and has no index to probe.
    """
    indexed = {
        row[0]: row[1]
        for row in conn.execute(
            f"SELECT fact_id, rowid FROM {FTS_TABLE}"
        ).fetchall()
    }
    fact_rows = conn.execute(
        "SELECT rowid, text, topic, id, scope FROM facts"
    ).fetchall()
    fact_ids = {row[3] for row in fact_rows}
    stale_rowids = [
        rowid for fact_id, rowid in indexed.items()
        if fact_id not in fact_ids
    ]
    if stale_rowids:
        conn.executemany(
            f"DELETE FROM {FTS_TABLE} WHERE rowid = ?",
            [(rowid,) for rowid in stale_rowids],
        )
    missing = [
        (row[0], row[1], row[2], row[3], row[4])
        for row in fact_rows
        if row[3] not in indexed
    ]
    if missing:
        conn.executemany(
            f"INSERT INTO {FTS_TABLE}(rowid, text, topic, fact_id, scope)"
            " VALUES (?, ?, ?, ?, ?)",
            missing,
        )
    return {"inserted": len(missing), "removed": len(stale_rowids)}


def build_match_query(query_keys: set[str], operator: str = "OR") -> str | None:
    """Build a safe FTS5 MATCH string from query keywords.

    Each keyword becomes a PREFIX term (`dark*`): FTS5's unicode61
    tokenizer strips diacritics ("café" -> "cafe") while the
    Python keyword() regex does not ("café" -> "caf"), so an exact
    term could miss a fact the Python scorer would have matched.
    Prefix terms make the FTS candidate set a SUPERSET of the old
    full-scan matches; the Python scorer is still authoritative
    and drops the extra candidates. Terms are joined with OR by
    default (a fact with any keyword overlap is a candidate) or
    with AND for the full-match fast path.

    Only [a-z0-9]+ terms are kept; anything else is dropped.
    Returns None when no valid term survives.
    """
    terms = sorted(t for t in query_keys if _TERM_RE.match(t))
    if not terms:
        return None
    joiner = f" {operator} "
    return joiner.join(f"{t}*" for t in terms)


def full_match_rows(
    conn: sqlite3.Connection,
    query_keys: set[str],
    scope: str | None = None,
    limit: int = 50,
) -> list:
    """Newest facts matching ALL query keywords (FTS5 AND),
    joined to `facts` and ordered newest-first (created_at DESC,
    rowid ASC — the same tiebreak the full sort produces).

    The recall fast path: every row here is a candidate for a
    1.0 overlap score, and a 1.0 can never be displaced by a
    partial match, so the newest `limit` verified 1.0 rows are
    the top results with no further scoring.
    """
    match = build_match_query(query_keys, operator="AND")
    if match is None:
        return []
    sql = (
        f"SELECT f.* FROM {FTS_TABLE}"
        f" JOIN facts f ON f.id = {FTS_TABLE}.fact_id"
        f" WHERE {FTS_TABLE} MATCH ?"
    )
    params: list = [match]
    if scope is not None:
        sql += f" AND {FTS_TABLE}.scope = ?"
        params.append(scope)
    sql += " ORDER BY f.created_at DESC, f.rowid ASC LIMIT ?"
    params.append(limit)
    return conn.execute(sql, params).fetchall()


def candidate_ids(
    conn: sqlite3.Connection,
    query_keys: set[str],
    scope: str | None = None,
    limit: int = CANDIDATE_LIMIT,
) -> list[str]:
    """Fact ids whose text/topic contains a query keyword.

    Scope filters FIRST (in the FTS query itself, on the
    UNINDEXED scope column) so a mission never pulls another
    mission's candidates. Candidates come back bm25-ranked so
    the LIMIT cap keeps the most relevant ones.
    """
    match = build_match_query(query_keys)
    if match is None:
        return []
    if scope is None:
        rows = conn.execute(
            f"SELECT fact_id FROM {FTS_TABLE}"
            f" WHERE {FTS_TABLE} MATCH ? ORDER BY rank LIMIT ?",
            (match, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT fact_id FROM {FTS_TABLE}"
            f" WHERE {FTS_TABLE} MATCH ? AND scope = ?"
            " ORDER BY rank LIMIT ?",
            (match, scope, limit),
        ).fetchall()
    return [row["fact_id"] for row in rows]


def fts_count(conn: sqlite3.Connection) -> int:
    """How many facts the index currently holds."""
    if not fts_exists(conn):
        return 0
    row = conn.execute(f"SELECT COUNT(*) AS c FROM {FTS_TABLE}").fetchone()
    return int(row["c"])
