"""Keyword-memory API (Sprint 2). Token-authenticated, like the rest.

POST /memory/facts  {text, source?, topic?}  -> 201 {id}
GET  /memory/facts?query=...&limit=5          -> {facts: [...]}
DELETE /memory/facts/{fact_id}                -> {ok, id} (404 when absent)

There is no list-all endpoint: facts are recalled by keyword
query only, never dumped.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ..core import db
from ..core.auth import require_token
from ..memory import keyword

router = APIRouter(tags=["memory"])


class FactIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)
    source: str = Field(default="user", max_length=120)
    topic: str | None = Field(default=None, max_length=120)


@router.post("/memory/facts", status_code=201)
def store_fact(body: FactIn, _token: str = Depends(require_token)):
    conn = db.connect()
    try:
        fact_id = keyword.store_fact(conn, body.text, body.source, body.topic)
    finally:
        conn.close()
    return {"id": fact_id}


@router.get("/memory/facts")
def recall_facts(
    query: str = Query(..., min_length=1, max_length=500),
    limit: int = Query(default=5, ge=1, le=20),
    _token: str = Depends(require_token),
):
    conn = db.connect()
    try:
        facts = keyword.recall(conn, query, limit=limit)
    finally:
        conn.close()
    return {"facts": facts}


@router.delete("/memory/facts/{fact_id}")
def delete_fact(fact_id: str, _token: str = Depends(require_token)):
    """Forget one fact (404 when it does not exist)."""
    conn = db.connect()
    try:
        forgotten = keyword.delete_fact(conn, fact_id)
    finally:
        conn.close()
    if not forgotten:
        raise HTTPException(status_code=404, detail=f"no fact {fact_id}")
    return {"ok": True, "id": fact_id}
