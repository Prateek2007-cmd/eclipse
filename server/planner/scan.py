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

    Returns a list of {type, value, start, end} hits — one per
    occurrence (the same value at two positions is two hits; both
    must be redacted). `raw_values` are values the redactor
    recorded (e.g. a name) that no regex can find — any of them
    surviving in the text is a hit. The spans let redact() replace
    by position instead of by value, so a short hit that is a
    substring of a longer hit elsewhere cannot mangle the longer
    replacement.
    """
    hits: list[dict] = []
    # Collect every match with its span, then resolve overlaps:
    # at any start position the LONGEST match wins (a 16-digit card
    # number's first 12 digits also match the Aadhaar pattern — the
    # card is the real hit, the Aadhaar is a false positive on the
    # same span). This mirrors the extension redactor's
    # earliest+longest rule.
    candidates: list[tuple[int, int, str, str]] = []
    for pii_type, pattern in PII_PATTERNS:
        for m in pattern.finditer(text):
            candidates.append((m.start(), m.end(), pii_type, m.group(0)))
    candidates.sort(key=lambda c: (c[0], -(c[1] - c[0])))
    used_until = -1
    for start, end, pii_type, value in candidates:
        if start < used_until:
            continue
        used_until = end
        hits.append({"type": pii_type, "value": value, "start": start, "end": end})
    for value in raw_values or []:
        if len(value) < 4:
            continue
        start = text.find(value)
        while start >= 0:
            hits.append(
                {
                    "type": "unredacted-value",
                    "value": value,
                    "start": start,
                    "end": start + len(value),
                }
            )
            start = text.find(value, start + len(value))
    hits.sort(key=lambda h: h["start"])
    return hits


def redact(text: str, raw_values: list[str] | None = None) -> tuple[str, list[dict]]:
    """Replace every hit with its placeholder; return (clean, hits).

    Replacement is by span, not by value: a shorter hit value that
    is a substring of a longer hit at a different position can no
    longer mangle the longer replacement (str.replace would have
    rewritten the inside of the longer value).
    """
    hits = scan(text, raw_values)
    if not hits:
        return text, []
    counters: dict[str, int] = {}
    parts: list[str] = []
    pos = 0
    for hit in hits:
        # Skip a hit that overlaps one already replaced (a raw
        # value can contain a pattern hit's span).
        if hit["start"] < pos:
            continue
        pii_type = hit["type"]
        counters[pii_type] = counters.get(pii_type, 0) + 1
        placeholder = _placeholder(pii_type, counters[pii_type])
        parts.append(text[pos : hit["start"]])
        parts.append(placeholder)
        pos = hit["end"]
        logger.warning(
            "second PII scan blocked %s (redacted to %s)", pii_type, placeholder
        )
    parts.append(text[pos:])
    return "".join(parts), hits
