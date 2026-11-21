"""Local embeddings for the RAG (Sprint 6, plan §6.6).

Model: sentence-transformers/all-MiniLM-L6-v2, 384 dimensions,
run locally via ONNX Runtime (no cloud, no API key). The model
file (~90MB) and the tokenizer live in server/rag/models/ and
are loaded once (lazy, on first use) and kept loaded (§6.10
fix 6: warm models, never reload per step).

Mean pooling over the token embeddings (attention-masked),
then L2 normalisation — the same pooling sentence-transformers
uses, so the vectors match the published model. A cosine
similarity between two normalised vectors is their dot product.

The model is NOT in the ≤300MB extension budget (it runs on the
server, not in the extension) and is NOT downloaded by the
extension — it is a server-side dependency, like the planner.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

import numpy as np

MODEL_DIR = Path(__file__).parent / "models"
MODEL_PATH = MODEL_DIR / "all-MiniLM-L6-v2.onnx"
TOKENIZER_PATH = MODEL_DIR / "tokenizer.json"
DIMENSIONS = 384
MAX_TOKENS = 256

_session = None
_tokenizer = None
_load_lock = threading.Lock()


def _load():
    global _session, _tokenizer
    if _session is not None:
        return _session, _tokenizer
    with _load_lock:
        if _session is not None:
            return _session, _tokenizer
        import onnxruntime as ort
        from tokenizers import Tokenizer

        if not MODEL_PATH.exists():
            raise RuntimeError(
                f"embeddings model not found at {MODEL_PATH} — "
                "download it from huggingface.co/sentence-transformers/all-MiniLM-L6-v2 (onnx/model.onnx + tokenizer.json)"
            )
        _session = ort.InferenceSession(str(MODEL_PATH), providers=["CPUExecutionProvider"])
        _tokenizer = Tokenizer.from_file(str(TOKENIZER_PATH))
        _tokenizer.enable_truncation(max_length=MAX_TOKENS)
        return _session, _tokenizer


def embed(text: str) -> np.ndarray:
    """One 384-dim, L2-normalised embedding for the text."""
    return embed_batch([text])[0]


def embed_batch(texts: list[str]) -> np.ndarray:
    """Embed a batch (shape (n, 384), L2-normalised rows)."""
    session, tokenizer = _load()
    encodings = [tokenizer.encode(t) for t in texts]
    max_len = max(len(e.ids) for e in encodings)
    batch = len(texts)
    input_ids = np.zeros((batch, max_len), dtype=np.int64)
    attention = np.zeros((batch, max_len), dtype=np.int64)
    token_type = np.zeros((batch, max_len), dtype=np.int64)
    for i, enc in enumerate(encodings):
        n = len(enc.ids)
        input_ids[i, :n] = enc.ids
        attention[i, :n] = enc.attention_mask
        token_type[i, :n] = enc.type_ids
    outputs = session.run(
        None,
        {
            "input_ids": input_ids,
            "attention_mask": attention,
            "token_type_ids": token_type,
        },
    )
    # Mean pooling over the token embeddings, attention-masked.
    token_embeddings = outputs[0]
    mask = attention[:, :, None].astype(np.float32)
    summed = (token_embeddings * mask).sum(axis=1)
    counts = mask.sum(axis=1)
    mean = summed / np.maximum(counts, 1e-9)
    norms = np.linalg.norm(mean, axis=1, keepdims=True)
    return (mean / np.maximum(norms, 1e-9)).astype(np.float32)


def model_available() -> bool:
    """Whether the model files are on disk."""
    return MODEL_PATH.exists() and TOKENIZER_PATH.exists()
