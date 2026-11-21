# Runbook: SQLite + sqlite-vec (RAG memory)

Eclipse's vector memory is SQLite + the `sqlite-vec` extension. No
separate vector database, no cloud. Embeddings are computed locally
with all-MiniLM-L6-v2 (ONNX, 384 dimensions) on the server.

## What lives where

- `data/eclipse.db` — the only SQLite file. `rag_chunks` holds the
  text (`id, text, kind, meta, created_at`); `rag_vectors` is a
  `vec0` virtual table (`id TEXT PRIMARY KEY, embedding float[384]`).
- `server/rag/models/` — the embedding model, **not in git**
  (gitignored, ~91 MB). Download it with
  `python3 scripts/download_rag_model.py` after cloning.
- `server/rag/embed.py` — loads the ONNX model once, keeps it loaded.
- `server/rag/store.py` — index / query / forget.

## The per-connection trap

sqlite-vec is loaded **per connection**. A table existing in the
file does **not** mean `vec0` is loaded on this connection. The
store therefore calls `_vec_conn(conn)` on **every** connection:
load the extension, then `CREATE VIRTUAL TABLE IF NOT EXISTS
rag_vectors USING vec0(...)`. A second load on one connection is a
no-op. Do not "optimise" this with a loaded-set keyed by
connection id — a fresh connection can reuse a dead connection's
id and skip the load (`no such table: rag_vectors`).

## Query path

1. `query()` returns `[]` early when the table is empty — no model
   call is wasted.
2. The query is embedded, then
   `SELECT id, distance FROM rag_vectors WHERE embedding MATCH ?
   AND k = ? ORDER BY distance`.
3. sqlite-vec returns L2 distance; vectors are L2-normalised, so
   cosine = `1 - distance²/2`. Results below the **0.6** threshold
   are dropped. Below 0.6 the answer is "no relevant memory".

## PII boundary (honest)

`index_chunk` refuses text that still matches a structured PII
pattern (email, card, Aadhaar, PAN, phone, date). That is a
**regex backstop, not a guarantee**: it does NOT catch names,
addresses, rolls, CVV, OTP or passwords (label/field-caught).
Callers must redact first. The store refuses; it does not promise.

## Forget

`DELETE /rag/{chunk_id}` removes the chunk from both tables. A
forgotten chunk is excluded from future retrieval. `POST
/memory/reset` wipes all memory after export + double confirm
(see the memory API).

## Troubleshooting

- `no such module: vec0` — the extension was not loaded on this
  connection. Check `_vec_conn` is called before the query.
- `no such table: rag_vectors` — same cause, or the table was
  never created (fresh DB without migrate).
- `503 model unavailable` from `/rag/index` — the model files are
  missing. Run `scripts/download_rag_model.py`.
- Slow first query (~1s) — the ONNX session loads lazily on first
  use; later queries are fast. This is by design (§6.10 fix 6:
  warm models, never reload per step).
