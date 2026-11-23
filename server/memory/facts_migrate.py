"""PR #5 fact-store migrations (W5).

Standalone: db.py is W1's file, so this module is NOT wired
into db.migrate() — the coordinator adds one
`facts_migrate.apply_facts_migrations(conn)` call inside
migrate() at integration. Until then, call it directly after
db.migrate() (the tests do exactly that).

What it does, in order:
1. Adds `fact_hash` TEXT and `scope` TEXT DEFAULT 'global' to
   `facts` (guarded by PRAGMA table_info, the db.py pattern).
   Existing rows get scope 'global' from the DEFAULT.
2. Backfills fact_hash for every row that lacks one (the hash
   is computed in Python — stock SQLite has no SHA-256).
3. Dedups pre-existing duplicates BEFORE the unique index is
   created (otherwise index creation fails on a dirty DB),
   keeping the latest-inserted row per (scope, fact_hash).
4. Creates the unique index on (scope, fact_hash) — dedup is a
   constraint, not a check (the mail_drafts precedent) — plus
   the scope/created_at indexes the scoped reads use.
5. Creates the FTS5 table + sync triggers and backfills the
   index from existing rows.

Idempotent: every step is guarded (IF NOT EXISTS, PRAGMA
checks, NULL-only backfill, NOT EXISTS FTS insert), so running
it twice is safe — the second run changes nothing. It works on
a database that already has facts (backfill + FTS populate).
"""
from __future__ import annotations

import sqlite3

from . import facts_fts
from .keyword import fact_hash, facts_columns


def apply_facts_migrations(conn: sqlite3.Connection) -> dict:
    """Apply the PR #5 fact-store migrations. Returns a report
    dict of what changed (all zeros on a second run)."""
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    if "facts" not in tables:
        raise ValueError("facts table missing — run db.migrate() first")
    report: dict = {
        "added_fact_hash": False,
        "added_scope": False,
        "hashed": 0,
        "deduped": 0,
        "fts_inserted": 0,
        "fts_removed": 0,
    }
    cols = facts_columns(conn)
    if "fact_hash" not in cols:
        conn.execute("ALTER TABLE facts ADD COLUMN fact_hash TEXT")
        report["added_fact_hash"] = True
    if "scope" not in cols:
        conn.execute(
            "ALTER TABLE facts ADD COLUMN scope TEXT DEFAULT 'global'"
        )
        report["added_scope"] = True
    # Backfill hashes for rows that lack one (NULL-only: safe to
    # re-run). Dedup FIRST, in Python: the unique index may
    # already exist (a second run, or a DB migrated by an older
    # version), so a batched UPDATE of two duplicate texts
    # would violate it mid-flight. Group NULL-hash rows by
    # (scope, hash), keep the latest-inserted row per group,
    # delete the rest, and drop any survivor whose (scope, hash)
    # already exists among hashed rows. Only survivors get an
    # UPDATE, so no UPDATE can conflict with the index.
    rows = conn.execute(
        "SELECT rowid, id, text, scope FROM facts"
        " WHERE fact_hash IS NULL"
    ).fetchall()
    existing = {
        (r["scope"], r["fact_hash"])
        for r in conn.execute(
            "SELECT scope, fact_hash FROM facts"
            " WHERE fact_hash IS NOT NULL"
        ).fetchall()
    }
    groups: dict = {}
    for row in rows:
        key = (row["scope"] or "global", fact_hash(row["text"]))
        groups.setdefault(key, []).append(row)
    delete_ids = []
    survivors = []
    for key, members in groups.items():
        members.sort(key=lambda r: r["rowid"])
        if key in existing:
            delete_ids.extend(m["id"] for m in members)
            continue
        delete_ids.extend(m["id"] for m in members[:-1])
        survivors.append((key[1], members[-1]["id"]))
    if delete_ids:
        conn.executemany(
            "DELETE FROM facts WHERE id = ?", [(i,) for i in delete_ids]
        )
    report["deduped"] = len(delete_ids)
    conn.executemany(
        "UPDATE facts SET fact_hash = ? WHERE id = ?", survivors
    )
    report["hashed"] = len(survivors)
    # Safety net for rows that already carried hashes and are
    # duplicates (only possible if they were written by raw SQL
    # while no unique index existed): keep the latest-inserted
    # row per (scope, fact_hash). Runs before index creation
    # below; a no-op when the index already exists.
    cur = conn.execute(
        "DELETE FROM facts WHERE fact_hash IS NOT NULL"
        " AND rowid NOT IN ("
        " SELECT MAX(rowid) FROM facts WHERE fact_hash IS NOT NULL"
        " GROUP BY scope, fact_hash)"
    )
    report["deduped"] += cur.rowcount
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_facts_scope_hash"
        " ON facts(scope, fact_hash)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_facts_scope_created"
        " ON facts(scope, created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_facts_created_at"
        " ON facts(created_at)"
    )
    facts_fts.ensure_fts(conn)
    backfill = facts_fts.backfill_fts(conn)
    report["fts_inserted"] = backfill["inserted"]
    report["fts_removed"] = backfill["removed"]
    conn.commit()
    return report
