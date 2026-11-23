"""Mission resume handling (PR #5, W2): store the user's resume
file server-side so missions can reference it, and extract its
text for matching and drafting — without new dependencies.

Storage: ECLIPSE_DATA_DIR/resumes/ (config.DATA_DIR), filename
sanitised (no path traversal — the stored name is built from a
basename allow-list plus a random suffix, never from user input
verbatim), files written 0600 (a resume is PII). Metadata lives
in the memory table under key "mission_resume": filename,
stored path, extracted_text (or null), extraction_status,
size, stored_at.

Extraction, stdlib only:
- .txt / .md: decoded as UTF-8 directly.
- .pdf: a minimal extractor over FlateDecode streams — find
  stream..endstream blocks, zlib-decompress, pull text from
  (...) Tj and [...] TJ operators, unescape. Best effort: it
  handles the common single-column text PDFs and misses
  embedded-font / hex-string / scanned-image PDFs.
- anything else: stored, extraction_status "unsupported".
When extraction fails or finds no text, the file is still
stored, extraction_status is "limited", extracted_text is
null, and the API says so — extracted text is never faked.

Only the latest resume is kept as "the" resume (the metadata
key holds one row); older files stay on disk until replaced —
store_resume deletes the previous stored file when a new one
lands, so stale PII does not pile up.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import uuid
import zlib
from pathlib import Path

from ..core import config

RESUME_KEY = "mission_resume"
RESUMES_DIRNAME = "resumes"
MAX_RESUME_BYTES = 5 * 1024 * 1024  # 5 MB
MAX_EXTRACTED_CHARS = 20000
EXTRACTABLE_EXTENSIONS = (".txt", ".md", ".pdf")

# Text-show operators: (string) Tj and [(string) n (string)] TJ.
_TJ_RE = re.compile(r"\(((?:[^()\\]|\\.)*)\)\s*Tj")
_TJ_ARRAY_RE = re.compile(r"\[(.*?)\]\s*TJ", re.DOTALL)
_TJ_STRING_RE = re.compile(r"\(((?:[^()\\]|\\.)*)\)")
_STREAM_RE = re.compile(rb"stream\r?\n(.*?)endstream", re.DOTALL)
_OCTAL_RE = re.compile(r"\\([0-7]{1,3})")


def resumes_dir() -> Path:
    """The resume directory (created on demand)."""
    path = config.DATA_DIR / RESUMES_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def sanitise_filename(name: str) -> str:
    """A safe stored filename: the basename only (no directory,
    no traversal), allow-listed characters, extension kept.
    Falls back to "resume" for an empty or fully-stripped name."""
    base = Path(name or "").name.strip()
    if not base or base in (".", ".."):
        return "resume"
    stem = Path(base).stem
    suffix = Path(base).suffix.lower()
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._")
    stem = re.sub(r"_+", "_", stem)[:80] or "resume"
    if suffix and re.fullmatch(r"\.[a-z0-9]{1,10}", suffix):
        return f"{stem}{suffix}"
    return stem


def _unescape_pdf_string(raw: str) -> str:
    """Unescape a PDF literal string's escapes (\\(, \\),
    \\\\, \\n, \\r, \\t, and octal \\ddd)."""
    def _octal(match):
        try:
            return chr(int(match.group(1), 8))
        except (ValueError, OverflowError):
            return ""

    text = _OCTAL_RE.sub(_octal, raw)
    for escaped, plain in (
        (r"\(", "("), (r"\)", ")"), (r"\\", "\\"),
        (r"\n", "\n"), (r"\r", "\r"), (r"\t", "\t"),
    ):
        text = text.replace(escaped, plain)
    return text


def _text_from_content(content: str) -> list[str]:
    """Text strings from one decompressed content stream, in
    order: TJ arrays first (their strings, concatenated), then
    bare Tj strings. A string matched by both patterns is
    counted once — the TJ pass blanks its matches before the
    Tj pass runs."""
    pieces: list[str] = []
    masked = content
    for array_match in _TJ_ARRAY_RE.finditer(content):
        inner = array_match.group(1)
        parts = [
            _unescape_pdf_string(m.group(1))
            for m in _TJ_STRING_RE.finditer(inner)
        ]
        if parts:
            pieces.append("".join(parts))
        start, end = array_match.span()
        masked = masked[:start] + " " * (end - start) + masked[end:]
    for match in _TJ_RE.finditer(masked):
        pieces.append(_unescape_pdf_string(match.group(1)))
    return pieces


def extract_pdf_text(data: bytes) -> str:
    """Minimal stdlib-only PDF text extraction (best effort).
    Returns "" when nothing extractable is found — the caller
    records that as "limited", never as text."""
    pieces: list[str] = []
    for stream_match in _STREAM_RE.finditer(data):
        raw = stream_match.group(1)
        try:
            content = zlib.decompress(raw)
        except zlib.error:
            content = raw  # uncompressed stream — try it as-is
        try:
            text = content.decode("latin-1")
        except UnicodeDecodeError:
            continue
        pieces.extend(_text_from_content(text))
    cleaned = " ".join(p.strip() for p in pieces if p.strip())
    return re.sub(r"[ \t]+", " ", cleaned).strip()


def extract_text(data: bytes, filename: str) -> tuple[str | None, str]:
    """Extract text from resume bytes. Returns
    (text_or_None, status) with status "ok" | "limited" |
    "unsupported". Never raises for bad input — a failure is
    "limited", not an exception."""
    suffix = Path(filename or "").suffix.lower()
    if suffix in (".txt", ".md"):
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("utf-8", errors="replace")
        text = text.strip()
        if not text:
            return None, "limited"
        return text[:MAX_EXTRACTED_CHARS], "ok"
    if suffix == ".pdf":
        try:
            text = extract_pdf_text(data)
        except Exception:  # noqa: BLE001 - any parse failure is "limited"
            return None, "limited"
        if not text:
            return None, "limited"
        return text[:MAX_EXTRACTED_CHARS], "ok"
    return None, "unsupported"


def get_resume_meta(conn: sqlite3.Connection) -> dict | None:
    """The stored resume's metadata, or None when no resume was
    uploaded yet."""
    row = conn.execute(
        "SELECT value FROM memory WHERE key = ?", (RESUME_KEY,)
    ).fetchone()
    if row is None:
        return None
    try:
        meta = json.loads(row["value"])
    except (ValueError, TypeError):
        return None
    return meta if isinstance(meta, dict) else None


def save_resume_meta(conn: sqlite3.Connection, meta: dict) -> None:
    conn.execute(
        "INSERT INTO memory (key, value, updated_at)"
        " VALUES (?, ?, strftime('%s','now'))"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
        " updated_at = strftime('%s','now')",
        (RESUME_KEY, json.dumps(meta)),
    )
    conn.commit()


def resume_file_path(meta: dict | None) -> str | None:
    """The stored file's path, when the metadata names a file
    that still exists on disk."""
    if not meta:
        return None
    path = meta.get("stored_path")
    if path and Path(path).is_file():
        return str(path)
    return None


def _write_private(path: Path, data: bytes) -> None:
    """Write a file 0600 from the first byte (a resume is PII)
    — the same os.open pattern as config.get_token."""
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def store_resume(conn: sqlite3.Connection, filename: str,
                 data: bytes) -> dict:
    """Store an uploaded resume file and extract its text.
    Returns the metadata dict (also saved to the memory table).
    Raises ValueError for an empty file or one over the size
    cap. The previous stored file is deleted when a new one
    lands."""
    if not data:
        raise ValueError("resume file is empty")
    if len(data) > MAX_RESUME_BYTES:
        raise ValueError(
            f"resume is over the {MAX_RESUME_BYTES // (1024 * 1024)} MB cap"
        )
    safe = sanitise_filename(filename)
    stem = Path(safe).stem
    suffix = Path(safe).suffix
    stored_name = f"{stem}-{uuid.uuid4().hex[:8]}{suffix}"
    target = resumes_dir() / stored_name
    _write_private(target, data)
    text, status = extract_text(data, safe)
    previous = get_resume_meta(conn)
    meta = {
        "filename": safe,
        "stored_name": stored_name,
        "stored_path": str(target),
        "size_bytes": len(data),
        "extraction_status": status,
        "extracted_text": text,
        "stored_at": int(time.time()),
    }
    save_resume_meta(conn, meta)
    if previous and previous.get("stored_path") not in (None, str(target)):
        try:
            Path(previous["stored_path"]).unlink(missing_ok=True)
        except OSError:
            pass  # a stale file is untidy, not fatal
    return meta


def store_resume_text(conn: sqlite3.Connection, text: str,
                      filename: str = "resume.txt") -> dict:
    """Store pasted resume text as a .txt file (extraction is
    trivially "ok" — the text IS the resume)."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("resume text must not be empty")
    if len(text) > MAX_EXTRACTED_CHARS:
        raise ValueError(
            f"resume text must be at most {MAX_EXTRACTED_CHARS} characters"
        )
    return store_resume(conn, filename, text.strip().encode("utf-8"))
