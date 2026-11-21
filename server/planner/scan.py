"""Server-side second PII scan (Sprint 3 fix, plan §6.2 step 3, §6.4).

The extension redacts before anything leaves the device, but the
server is the party making the outbound planner call — so it must
rescan the assembled prompt with the same PII patterns (mirroring
extension/src/vision/pii.ts) plus the raw values the redactor
recorded. Any hit → block, re-redact (replace with the placeholder),
log. Two blocks on the same step → stop the task.

This is the second scan, not the first: the prompt should already
be redacted. A hit here means the extension layer leaked (Sprint 2's
own review proved that layer can leak), or the goal / memory facts
carried raw PII (facts are user-stated, stored verbatim). Either
way the value must not reach the planner.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger("eclipse.planner")

# Mirrors PII_PATTERNS in extension/src/vision/pii.ts. Order matters:
# card before generic 16-digit runs, Aadhaar before generic 12-digit.
PII_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("card_number", re.compile(r"\b(?:\d{4}[\s-]?){3}\d{4}\b")),
    ("aadhaar", re.compile(r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}\b")),
    ("pan", re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b", re.IGNORECASE)),
    ("phone", re.compile(r"(?:\+91[\s-]?)?[6-9]\d{4}[\s-]?\d{5}\b")),
    ("dob", re.compile(
        r"\b\d{2}\s(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s\d{4}\b"
        r"|\b\d{2}/\d{2}/\d{4}\b",
        re.IGNORECASE,
    )),
]

# Mirrors the fixed placeholders in pii.ts PlaceholderRegistry.
FIXED_PLACEHOLDERS = {
    "card_number": "[CARD_LAST4]",
    "cvv": "[CVV]",
    "otp": "[OTP]",
    "password": "[PASSWORD_1]",
}


def _placeholder(pii_type: str, n: int) -> str:
    if pii_type in FIXED_PLACEHOLDERS:
        return FIXED_PLACEHOLDERS[pii_type]
    return f"[{pii_type.upper()}_{n}]"


def scan(text: str, raw_values: list[str] | None = None) -> list[dict]:
    """Find structured PII and recorded raw values in `text`.

    Returns a list of {type, value} hits (deduplicated by value).
    `raw_values` are values the redactor recorded (e.g. a name) that
    no regex can find — any of them surviving in the text is a hit.
    """
    hits: list[dict] = []
    seen: set[str] = set()
    for pii_type, pattern in PII_PATTERNS:
        for m in pattern.finditer(text):
            value = m.group(0)
            if value not in seen:
                seen.add(value)
                hits.append({"type": pii_type, "value": value})
    for value in raw_values or []:
        if len(value) >= 4 and value in text and value not in seen:
            seen.add(value)
            hits.append({"type": "unredacted-value", "value": value})
    return hits


def redact(text: str, raw_values: list[str] | None = None) -> tuple[str, list[dict]]:
    """Replace every hit with its placeholder; return (clean, hits)."""
    hits = scan(text, raw_values)
    if not hits:
        return text, []
    clean = text
    counters: dict[str, int] = {}
    for hit in hits:
        pii_type = hit["type"]
        counters[pii_type] = counters.get(pii_type, 0) + 1
        placeholder = _placeholder(pii_type, counters[pii_type])
        clean = clean.replace(hit["value"], placeholder)
        logger.warning(
            "second PII scan blocked %s (redacted to %s)", pii_type, placeholder
        )
    return clean, hits
