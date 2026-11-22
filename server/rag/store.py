"""The vector RAG store (Sprint 6, plan §6.6).

SQLite + sqlite-vec, local only. The chunks are REDACTED text
only — the redactor runs before the index, and the store refuses
text that still matches a PII pattern. The boundary is honest:
the refusal is a REGEX scan, so it catches structured PII
(emails, cards, Aadhaar, PAN, phones, dates) but NOT names,
addresses, rolls, CVV, OTP or passwords — those are caught by
label/field, not by regex, and the store cannot detect them. The
callers must redact first; the store is a backstop, not a
guarantee. Each chunk is a fact, a memory, or a task observation
summary, keyed by a stable id so the same fact overwrites instead
of duplicating (the keyword memory's dedup-by-key rule).

Relevance threshold: cosine ≥ 0.6. Below it, the query returns
"no relevant memory" instead of a weak match (a weak match is
worse than none — it injects noise into the planner prompt).

Forget: DELETE /rag/{chunk_id} removes the chunk and its
vector, so it is excluded from future retrieval. Keyword facts
are forgotten separately via DELETE /memory/facts/{fact_id} —
a fact and a chunk are separate rows, so deleting one does not
delete the other. There is no whole-memory reset endpoint.

The vector table is a sqlite-vec virtual table (vec0) with 384
dimensions, float32. The chunks table holds the text + metadata;
the vector table holds the embedding, keyed by the chunk id.
"""
from __future__ import annotations

import json
import sqlite3

import numpy as np

from ..core import db as db_mod
from ..planner import scan as pii_scan
from . import embed as embed_mod

RELEVANCE_THRESHOLD = 0.6
DIMENSIONS = 384

def _vec_conn(conn: sqlite3.Connection) -> sqlite3.Connection:
    """Load the sqlite-vec extension on this connection and make
    sure the vector table exists. The extension is per-connection:
    the table existing in the file does NOT mean vec0 is loaded
    on this connection, so the load runs every time (it is a
    no-op when already loaded)."""
    import sqlite_vec

    conn.enable_load_extension(True)
    try:
        # Loading twice on one connection is a no-op, so no guard
        # is needed — and no exception is swallowed here: a genuine
        # load failure (missing .so) must surface, not hide behind
        # a confusing "no such module: vec0" from the CREATE below.
        sqlite_vec.load(conn)
    finally:
        conn.enable_load_extension(False)
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS rag_vectors USING vec0("
        "id TEXT PRIMARY KEY, embedding float[384])"
    )
    return conn


def index_chunk(conn, chunk_id: str, text: str, kind: str = "fact",
                meta: dict | None = None) -> bool:
    """Index one redacted chunk. Returns False when the text is
    refused (it still matches a PII pattern — redact first)."""
    if not text or not text.strip():
        return False
    # Redacted text only: refuse anything that still matches a
    # PII pattern (the redactor runs before the index, and this is
    # the second line of defence).
    hits = pii_scan.scan(text)
    if hits:
        return False
    _vec_conn(conn)
    vector = embed_mod.embed(text)
    conn.execute(
        "INSERT INTO rag_chunks (id, text, kind, meta, created_at)"
        " VALUES (?, ?, ?, ?, strftime('%s','now'))"
        " ON CONFLICT(id) DO UPDATE SET text = excluded.text,"
        " kind = excluded.kind, meta = excluded.meta",
        (chunk_id, text, kind, json.dumps(meta or {})),
    )
    # vec0 does not support UPSERT — delete then insert.
    conn.execute("DELETE FROM rag_vectors WHERE id = ?", (chunk_id,))
    conn.execute(
        "INSERT INTO rag_vectors (id, embedding) VALUES (?, ?)",
        (chunk_id, vector.tobytes()),
    )
    conn.commit()
    return True


def query(conn, text: str, limit: int = 5) -> list[dict]:
    """The most relevant chunks for the query (cosine ≥ 0.6).
    Returns [] when nothing is relevant — never a weak match."""
    if not text or not text.strip():
        return []
    _vec_conn(conn)
    # No chunks indexed — don't waste a model call on an empty table.
    if count(conn) == 0:
        return []
    vector = embed_mod.embed(text)
    rows = conn.execute(
        "SELECT id, distance FROM rag_vectors"
        " WHERE embedding MATCH ? AND k = ?"
        " ORDER BY distance",
        (vector.tobytes(), limit),
    ).fetchall()
    # sqlite-vec returns the L2 distance for normalised vectors;
    # cosine = 1 - distance²/2. Filter at the 0.6 threshold.
    results = []
    for row in rows:
        distance = float(row["distance"])
        cosine = 1.0 - (distance * distance) / 2.0
        if cosine < RELEVANCE_THRESHOLD:
            continue
        chunk = conn.execute(
            "SELECT text, kind, meta FROM rag_chunks WHERE id = ?",
            (row["id"],),
        ).fetchone()
        if chunk is None:
            continue
        results.append({
            "id": row["id"],
            "text": chunk["text"],
            "kind": chunk["kind"],
            "meta": json.loads(chunk["meta"] or "{}"),
            "score": round(cosine, 3),
        })
    return results


def forget(conn, chunk_id: str) -> bool:
    """Remove a chunk (and its vector) — it is excluded from
    future retrieval."""
    _vec_conn(conn)
    cur = conn.execute("DELETE FROM rag_chunks WHERE id = ?", (chunk_id,))
    conn.execute("DELETE FROM rag_vectors WHERE id = ?", (chunk_id,))
    conn.commit()
    return cur.rowcount > 0


def count(conn) -> int:
    """How many chunks are indexed."""
    _vec_conn(conn)
    row = conn.execute("SELECT COUNT(*) AS c FROM rag_chunks").fetchone()
    return int(row["c"])
