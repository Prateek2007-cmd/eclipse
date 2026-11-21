#!/usr/bin/env python3
"""Download Eclipse's on-device vision models (Sprint 2).

Models are gitignored and fetched on first use — never committed.
Licences are stated in extension/models/README.md.

Sources (verified live 2026-11-21):
  YOLO26n     Ultralytics assets release (AGPL-3.0)
  BlazeFace   garavv/blazeface-onnx on Hugging Face (Apache-2.0)
  PP-OCRv4/3  rapidocr-onnxruntime PyPI wheel (Apache-2.0, PaddleOCR)

Usage: python3 scripts/download_models.py [--out extension/models]
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
import urllib.request
import zipfile
from pathlib import Path

UA = {"User-Agent": "eclipse/0.1 (+https://github.com/akashgoudsidduluri/eclipse)"}

MODELS = [
    {
        "name": "yolo26n.onnx",
        "urls": [
            "https://github.com/ultralytics/assets/releases/download/v8.4.0/yolo26n.onnx",
            "https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo26n.onnx",
        ],
        # Pinned sha256 of the downloaded file (verified 2026-11-21).
        # If the upstream file changes, the download fails loudly
        # instead of silently shipping a different model.
        "sha256": "2e947b787d9e787b93a16772a5f55b1d4d8c4d86f53146149c5d6a642442d6f7",
    },
    {
        "name": "blazeface.onnx",
        "urls": [
            "https://huggingface.co/garavv/blazeface-onnx/resolve/main/blaze.onnx?download=true",
        ],
        "sha256": "564740c5146673c840257402cee8309161848e48e64d277a862ab4d501adf8a5",
    },
]

# Pinned sha256s of the PP-OCR files inside the rapidocr-onnxruntime
# wheel (version 1.4.4, verified 2026-11-21). A wheel update changes
# these — the script then fails loudly rather than shipping a new
# model under the old name.
PPOCR_SHA256 = {
    "ch_PP-OCRv4_det_infer.onnx": "d2a7720d45a54257208b1e13e36a8479894cb74155a5efe29462512d42f49da9",
    "ch_PP-OCRv4_rec_infer.onnx": "48fc40f24f6d2a207a2b1091d3437eb3cc3eb6b676dc3ef9c37384005483683b",
    "ch_ppocr_mobile_v2.0_cls_infer.onnx": "e47acedf663230f8863ff1ab0e64dd2d82b838fceb5957146dab185a89d6215c",
}

# The rec model's embedded dictionary must have exactly this many
# characters — the decode list is built from it, and a wrong count
# means a wrong decode order.
EXPECTED_REC_CHARS = 6623


def fetch(url: str, timeout: int = 120) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status} for {url}")
        return resp.read()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verify_file(path: Path, expected: str) -> bool:
    """True when the file exists and matches its pinned sha256.
    A pre-existing partial or corrupt file is NOT accepted as
    "exists" — it is re-downloaded by the caller."""
    if not path.exists():
        return False
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != expected:
        print(f"  {path.name}: hash mismatch (have {actual[:16]}…, want {expected[:16]}…) — re-downloading")
        return False
    return True


def download_rapidocr_models(out: Path) -> list[str]:
    """Extract the PP-OCR ONNX files from the rapidocr-onnxruntime wheel."""
    req = urllib.request.Request(
        "https://pypi.org/pypi/rapidocr-onnxruntime/json", headers=UA
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        meta = json.load(resp)
    wheel_url = None
    for entry in meta["urls"]:
        if entry["filename"].endswith("-py3-none-any.whl"):
            wheel_url = entry["url"]
            break
    if not wheel_url:
        raise RuntimeError("rapidocr-onnxruntime wheel not found on PyPI")
    print(f"  wheel: {meta['info']['version']}")
    data = fetch(wheel_url)
    saved: list[str] = []
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for name in zf.namelist():
            base = Path(name).name
            if base in PPOCR_SHA256:
                target = out / base
                target.write_bytes(zf.read(name))
                if not verify_file(target, PPOCR_SHA256[base]):
                    raise RuntimeError(f"{base}: hash mismatch after extraction")
                saved.append(base)
                print(f"  saved {base} ({target.stat().st_size/1e6:.1f} MB, sha256 verified)")
    if not saved:
        raise RuntimeError("no PP-OCR .onnx files inside the wheel")
    return saved


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="extension/models")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    for spec in MODELS:
        target = out / spec["name"]
        if verify_file(target, spec["sha256"]):
            print(f"exists: {spec['name']} (sha256 verified) — skipping")
            continue
        got = None
        last_err = None
        for url in spec["urls"]:
            try:
                print(f"fetching {spec['name']} from {url}")
                got = fetch(url)
                break
            except Exception as exc:  # noqa: BLE001 - try next mirror
                last_err = exc
                print(f"  failed: {exc}")
        if got is None:
            # No fallback: yolo11n has a different output shape and
            # every caller references yolo26n.onnx by name — a silent
            # fallback would ship a file nothing loads. Fail loudly.
            print(f"ERROR: could not fetch {spec['name']}: {last_err}", file=sys.stderr)
            return 1
        target.write_bytes(got)
        if not verify_file(target, spec["sha256"]):
            print(f"ERROR: {spec['name']} hash mismatch after download", file=sys.stderr)
            return 1
        print(f"saved {target.name} ({len(got)/1e6:.1f} MB, sha256 verified)")

    print("fetching PP-OCR models from the rapidocr-onnxruntime wheel…")
    download_rapidocr_models(out)
    # The rec model embeds its 6623-char dictionary in its ONNX
    # metadata ("character"). Extract it — RapidOCR reads the same
    # metadata at runtime, so this order is exactly what the model
    # expects. (The public ppocr_keys_v1.txt is a different order.)
    extract_rec_chars(out / "ch_PP-OCRv4_rec_infer.onnx", out / "ppocr_rec_chars.txt")
    print("done.")
    return 0


def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
    return result, pos


def extract_rec_chars(model_path: Path, target: Path) -> None:
    """Extract the embedded 'character' metadata from the rec model."""
    data = model_path.read_bytes()
    i = data.find(b"character")
    if i < 0:
        raise RuntimeError("no 'character' metadata in rec model")
    ln, p = _read_varint(data, i - 1)
    vlen, vp = _read_varint(data, p + ln + 1)
    text = data[vp : vp + vlen].decode("utf-8")
    chars = text.split("\n")
    if chars and chars[-1] == "":
        chars.pop()
    if len(chars) != EXPECTED_REC_CHARS:
        raise RuntimeError(
            f"rec char list has {len(chars)} entries, expected {EXPECTED_REC_CHARS} — "
            "the decode order would be wrong"
        )
    target.write_text(text, encoding="utf-8")
    print(f"saved ppocr_rec_chars.txt ({len(chars)} chars, from model metadata)")


if __name__ == "__main__":
    raise SystemExit(main())
