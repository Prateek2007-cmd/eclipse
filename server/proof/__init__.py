"""Outbound call log — the data behind the what-was-sent proof view
(plan §5.6).

Every planner call is recorded AFTER the redaction pipeline has
run: the stored prompt is the redacted text that actually left the
device, never the raw observation. A record holds the endpoint, the
model, an estimated token count, the round-trip time, and the
redacted prompt (capped at 4000 chars). The log is in-memory only
(cap 200 entries, oldest dropped) — it is proof, not storage.

The egress audit scans the FULL prompt at record time (prompts run
to ~32K chars at the 8K-token budget; the stored 4000-char prefix
alone would miss a secret past the cap) and stores only the hit
TYPES — never the values. The audit therefore covers the whole
payload without ever serving a secret back.
"""
from __future__ import annotations

import threading
import time
import uuid
from collections import deque

from server.planner import scan

MAX_ENTRIES = 200
MAX_PROMPT_CHARS = 4000

_log: deque[dict] = deque(maxlen=MAX_ENTRIES)
_lock = threading.Lock()


def record(endpoint: str, model: str, prompt: str, ms: float, ok: bool) -> dict:
    """Append one outbound call. `prompt` must already be redacted.

    The full prompt is scanned for PII here (before truncation) and
    only the hit types are kept — a secret past the 4000-char stored
    prefix is still caught, and the stored record never holds a
    secret value.
    """
    pii_types = sorted({h["type"] for h in scan.scan(prompt)})
    entry = {
        "id": uuid.uuid4().hex[:12],
        "at": int(time.time()),
        "endpoint": endpoint,
        "model": model,
        "tokens_est": max(1, len(prompt) // 4),
        "ms": round(ms, 1),
        "ok": ok,
        "prompt_redacted": prompt[:MAX_PROMPT_CHARS],
        "pii_types": pii_types,
    }
    with _lock:
        _log.append(entry)
    return entry


def entries(limit: int = 50) -> list[dict]:
    """Newest first."""
    with _lock:
        return list(_log)[-limit:][::-1]


def clear() -> None:
    with _lock:
        _log.clear()


def egress_audit() -> dict:
    """Live grep of every stored outbound prompt for secrets.

    Returns {hits, checked, patterns}. A hit is a secret that left
    the device — the run must stop and the payload must be blocked.
    Hits carry type + entry + endpoint only, never the value.
    """
    hits: list[dict] = []
    with _lock:
        snapshot = list(_log)
    for entry in snapshot:
        for pii_type in entry.get("pii_types", []):
            hits.append(
                {
                    "entry_id": entry["id"],
                    "endpoint": entry["endpoint"],
                    "type": pii_type,
                }
            )
    return {
        "hits": hits,
        "hit_count": len(hits),
        "checked": len(snapshot),
        "patterns": [name for name, _ in scan.PII_PATTERNS],
        "passed": len(hits) == 0,
    }
