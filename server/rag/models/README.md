# Server RAG models — not in the repo

The embeddings model is downloaded, never committed (~91 MB of
binary files; the snapshot push is UTF-8 text only).

- Model: sentence-transformers/all-MiniLM-L6-v2 (ONNX)
- Files: `all-MiniLM-L6-v2.onnx` (90,405,214 bytes), `tokenizer.json`
  (466,247 bytes), `config.json` (612 bytes)
- Source: https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2
  (the `.onnx` file is the repo's `onnx/model.onnx`)
- Download: `python3 scripts/download_rag_model.py` (or copy the three
  files into this directory by hand)
- Used by: `server/rag/embed.py` — 384-dim embeddings, mean pooling +
  L2 normalisation, cosine threshold 0.6 in `server/rag/store.py`
