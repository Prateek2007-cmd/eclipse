"""One SQLite file, one writer (the server). WAL mode.

Schema is section 6.2 of the finalised plan. Vault items are never in
SQLite. Audit log is append-only.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks(
  id TEXT PRIMARY KEY, goal TEXT, constraints TEXT, status TEXT,
  created_at INTEGER, updated_at INTEGER, receipt TEXT);
CREATE TABLE IF NOT EXISTS steps(
  id TEXT PRIMARY KEY, task_id TEXT, n INTEGER, action_json TEXT,
  status TEXT, started_at INTEGER, ended_at INTEGER, ms INTEGER,
  element_id TEXT, result TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS checkpoints(
  id TEXT PRIMARY KEY, task_id TEXT, step_n INTEGER, summary TEXT,
  state_json TEXT, created_at INTEGER);
CREATE TABLE IF NOT EXISTS memory(
  key TEXT PRIMARY KEY, value TEXT, kind TEXT, updated_at INTEGER);
CREATE TABLE IF NOT EXISTS facts(
  id TEXT PRIMARY KEY, text TEXT, source TEXT, created_at INTEGER);
CREATE TABLE IF NOT EXISTS routines(
  id TEXT PRIMARY KEY, name TEXT, trigger TEXT, action TEXT);
CREATE TABLE IF NOT EXISTS audit_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, action TEXT,
  payload_hash TEXT, prev_hash TEXT, hash TEXT, created_at INTEGER);
CREATE TABLE IF NOT EXISTS rag_chunks(
  id TEXT PRIMARY KEY, doc_id TEXT, chunk TEXT, vec BLOB,
  created_at INTEGER);
CREATE TABLE IF NOT EXISTS watches(
  id TEXT PRIMARY KEY, url TEXT, selector TEXT, last_value TEXT,
  threshold TEXT, status TEXT, muted_until INTEGER, created_at INTEGER);
CREATE TABLE IF NOT EXISTS grants(
  id TEXT PRIMARY KEY, origin TEXT, scope TEXT, expires_at INTEGER,
  created_at INTEGER);
CREATE TABLE IF NOT EXISTS quota(
  model TEXT PRIMARY KEY, used_today INTEGER, limit_day INTEGER,
  reset_at INTEGER);
"""

# Verified free top 5 (Sprint 0 probe, 2026-11-21). gpt-oss-120b is
# tracked here but returns empty content live, so it is not the planner.
# 200/day is the user's verified free limit for these models.
FREE_MODELS = [
    "gpt-oss-120b",
    "google-gemma-4-31b-it",
    "nvidia-nemotron-3-nano-30b-a3b",
    "google-gemma-4-26b-a4b-it",
    "mistral-small-3-2-24b-instruct",
]

_write_lock = threading.Lock()


def _next_midnight_utc_epoch() -> int:
    now = datetime.now(timezone.utc)
    midnight = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    return int(midnight.timestamp()) + 86400


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    """Open a connection to the Eclipse SQLite file (WAL mode)."""
    path = db_path or (config.DATA_DIR / "eclipse.db")
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    # Wait up to 5s on write contention instead of failing at once.
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


def migrate(db_path: Path | None = None) -> None:
    """Create the schema if missing; seed and roll over the quota table."""
    with _write_lock:
        conn = connect(db_path)
        try:
            conn.executescript(SCHEMA)
            reset_at = _next_midnight_utc_epoch()
            for model in FREE_MODELS:
                conn.execute(
                    "INSERT OR IGNORE INTO quota"
                    "(model, used_today, limit_day, reset_at)"
                    " VALUES (?, 0, 200, ?)",
                    (model, reset_at),
                )
            # Daily rollover: a reset_at in the past means a new day.
            now = int(time.time())
            conn.execute(
                "UPDATE quota SET used_today = 0, reset_at = ?"
                " WHERE reset_at <= ?",
                (reset_at, now),
            )
            conn.commit()
        finally:
            conn.close()
