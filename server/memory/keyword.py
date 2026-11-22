"""Keyword memory (Sprint 2).

Facts keyed by topic, recalled by keyword overlap. This is the
lightweight memory the plan wants from Sprint 2 so recall works
early; vector RAG lands in Sprint 6. The server is the only writer.

Scoring: a fact scores the fraction of the query's keywords it
contains (stopwords dropped). Ties break by newest first.
"""
from __future__ import annotations

import re
import sqlite3
import time
import uuid

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


def keywords(text: str) -> set[str]:
    """Lowercased word tokens with stopwords dropped."""
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    return {t for t in tokens if t not in STOPWORDS and len(t) > 1}


def store_fact(
    conn: sqlite3.Connection,
    text: str,
    source: str = "user",
    topic: str | None = None,
) -> str:
    """Store one fact. Returns its id."""
    fact_id = f"fact-{uuid.uuid4().hex[:12]}"
    conn.execute(
        "INSERT INTO facts(id, text, source, topic, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (fact_id, text, source, topic, int(time.time())),
    )
    conn.commit()
    return fact_id


def delete_fact(conn: sqlite3.Connection, fact_id: str) -> bool:
    """Forget one fact. Returns False when no such fact exists.
    A fact and a RAG chunk are separate rows — this removes the
    fact only; a chunk with the same id (if one was indexed)
    is forgotten via DELETE /rag/{chunk_id}."""
    cur = conn.execute("DELETE FROM facts WHERE id = ?", (fact_id,))
    conn.commit()
    return cur.rowcount > 0


def recall(
    conn: sqlite3.Connection, query: str, limit: int = 5
) -> list[dict]:
    """Recall facts by keyword overlap with the query."""
    query_keys = keywords(query)
    if not query_keys:
        return []
    rows = conn.execute(
        "SELECT id, text, source, topic, created_at FROM facts"
    ).fetchall()
    scored: list[tuple[float, int, dict]] = []
    for row in rows:
        fact_keys = keywords(row["text"]) | keywords(row["topic"] or "")
        overlap = len(query_keys & fact_keys)
        if overlap == 0:
            continue
        score = overlap / len(query_keys)
        scored.append(
            (
                score,
                row["created_at"],
                {
                    "id": row["id"],
                    "text": row["text"],
                    "source": row["source"],
                    "topic": row["topic"],
                    "score": round(score, 3),
                },
            )
        )
    scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
    return [s[2] for s in scored[:limit]]
