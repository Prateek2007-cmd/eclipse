"""RAG API (Sprint 6, plan §6.6).

- POST /rag/index {id, text, kind?, meta?} → index one redacted
  chunk (refused when the text still matches a PII pattern).
- GET /rag/query?q=&limit= → the most relevant chunks (cosine
  ≥ 0.6), or [] when nothing is relevant — never a weak match.
- DELETE /rag/{chunk_id} → forget the chunk (excluded from
  future retrieval).
- GET /rag/status → how many chunks are indexed, whether the
  model is available.

All token-gated. The chunks are redacted text only; the store
refuses text that still matches a PII pattern (server/rag/
store.py) — the refusal is regex-only, so names, addresses,
rolls, CVV, OTP and passwords are NOT caught by the store; the
caller must redact first. The embeddings are local
(all-MiniLM-L6-v2 via ONNX, no cloud, no API key).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..core import db
from ..core.auth import require_token
from ..rag import embed as embed_mod
from ..rag import store as rag_store

router = APIRouter()


class IndexIn(BaseModel):
    id: str = Field(..., min_length=1, max_length=200)
    text: str = Field(..., min_length=1, max_length=5000)
    kind: str = Field(default="fact", max_length=50)
    meta: dict = Field(default_factory=dict)


@router.post("/rag/index")
def index_chunk(body: IndexIn, _token: str = Depends(require_token)):
    """Index one redacted chunk. 409 when the text still matches
    a PII pattern (redact first)."""
    conn = db.connect()
    try:
        if not embed_mod.model_available():
            raise HTTPException(
                status_code=503,
                detail="embeddings model not available (server/rag/models/)",
            )
        ok = rag_store.index_chunk(conn, body.id, body.text, body.kind, body.meta)
    finally:
        conn.close()
    if not ok:
        raise HTTPException(
            status_code=409,
            detail="text refused: it still matches a PII pattern (redact first)",
        )
    return {"ok": True, "id": body.id}


@router.get("/rag/query")
def query_rag(q: str = "", limit: int = 5, _token: str = Depends(require_token)):
    """The most relevant chunks (cosine ≥ 0.6), or [] when
    nothing is relevant — never a weak match."""
    conn = db.connect()
    try:
        if not embed_mod.model_available():
            return {"results": [], "model_available": False}
        results = rag_store.query(conn, q, limit=max(1, min(limit, 20)))
    finally:
        conn.close()
    return {"results": results, "model_available": True}


@router.delete("/rag/{chunk_id}")
def forget_chunk(chunk_id: str, _token: str = Depends(require_token)):
    """Forget a chunk — it is excluded from future retrieval."""
    conn = db.connect()
    try:
        ok = rag_store.forget(conn, chunk_id)
    finally:
        conn.close()
    if not ok:
        raise HTTPException(status_code=404, detail=f"no chunk {chunk_id}")
    return {"ok": True, "id": chunk_id}


@router.get("/rag/status")
def rag_status(_token: str = Depends(require_token)):
    conn = db.connect()
    try:
        count = rag_store.count(conn)
    finally:
        conn.close()
    return {
        "chunks": count,
        "model": "all-MiniLM-L6-v2",
        "dimensions": 384,
        "threshold": rag_store.RELEVANCE_THRESHOLD,
        "model_available": embed_mod.model_available(),
    }
