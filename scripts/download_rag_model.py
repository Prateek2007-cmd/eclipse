#!/usr/bin/env python3
"""Download the server RAG embeddings model (Sprint 6).

Downloads sentence-transformers/all-MiniLM-L6-v2 (ONNX) from
Hugging Face into server/rag/models/. The model is ~91 MB and is
never committed — it is gitignored.

Usage:
    python3 scripts/download_rag_model.py
"""
from __future__ import annotations

import hashlib
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODELS_DIR = ROOT / "server" / "rag" / "models"

FILES = {
    "all-MiniLM-L6-v2.onnx": (
        "https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2"
        "/resolve/main/onnx/model.onnx",
        90405214,
    ),
    "tokenizer.json": (
        "https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2"
        "/resolve/main/tokenizer.json",
        466247,
    ),
    "config.json": (
        "https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2"
        "/resolve/main/config.json",
        612,
    ),
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    for name, (url, size) in FILES.items():
        dest = MODELS_DIR / name
        if dest.exists() and dest.stat().st_size == size:
            print(f"{name}: already present ({size} bytes), skipping")
            continue
        print(f"{name}: downloading from {url} ...")
        urllib.request.urlretrieve(url, dest)
        got = dest.stat().st_size
        if got != size:
            print(f"ERROR: {name} is {got} bytes, expected {size}", file=sys.stderr)
            return 1
        print(f"{name}: {got} bytes, sha256 {_sha256(dest)[:16]}...")
    print("done — model ready in server/rag/models/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
