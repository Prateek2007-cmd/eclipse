"""Mission intake (PR #5, §3.3): one chat message -> a mission
draft, for the user to confirm before anything is created. The
intake endpoint never creates a mission.

Two paths, always labelled:
- planner: the message (PII-redacted FIRST — see below) goes to
  the planner with the intake prompt; the JSON draft is
  validated against the type registry. A valid draft returns
  source "planner".
- rules: deterministic keyword + regex extraction, local only.
  Used when the planner fails (PlannerError), returns invalid
  JSON, or returns a draft that fails validation. Source
  "rules", and the warnings say why.

PII: the planner path redacts the message with the server-side
second PII scan (server/planner/scan.py) BEFORE the prompt is
built — the prompt passed to call_fn contains no raw email or
phone (asserted in tests). The rules path runs locally on the
original message (it never leaves the device), so it can
extract a recipient address the user typed. Never silently fall
back to a mock planner: PlannerError means rules, labelled.

parse_intake returns:
  {"draft": {"type", "name", "config", "bounds"},
   "source": "planner" | "rules",
   "confidence": "high" | "low",
   "missing_fields": [...], "warnings": [...]}
`missing_fields` names config/bounds fields the message did not
say enough to fill — the UI prompts for them. The draft's
config may hold None for a missing required field (validated
with validate_config_draft); create-time validation still
requires it.
"""
from __future__ import annotations

import json
import logging
import re
from urllib.parse import urlparse

from ..planner import prompts, router, scan
from . import types as _types

logger = logging.getLogger("eclipse.missions")

MAX_MESSAGE_LEN = 2000

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_URL_RE = re.compile(r"https?://[^\s<>\")\]]+")
_MONEY_RE = re.compile(
    r"(?:₹|Rs\.?|INR)\s*(\d[\d,]*(?:\.\d+)?)\s*(k)?", re.IGNORECASE
)
_TARGET_PRICE_RE = re.compile(
    r"(?:below|under|drops?\s+to|less\s+than|at\s+most)\s+₹?\s*"
    r"(\d[\d,]*(?:\.\d+)?)\s*(k)?",
    re.IGNORECASE,
)
_STIPEND_RE = re.compile(
    # The gap excludes commas: "min Rs 10,000 stipend, apply
    # to at most 3 a day" must NOT skip the comma and capture
    # the 3 as the stipend (PR #6 verification found ₹3).
    r"stipend[^0-9,]{0,20}₹?\s*(\d[\d,]*)\s*(k)?", re.IGNORECASE
)
# The number can come first: "min 10k stipend", "15000 stipend".
_STIPEND_AFTER_RE = re.compile(
    r"(\d[\d,]*)\s*(k)?\s*stipend", re.IGNORECASE
)
_CAP_RE = re.compile(
    r"(?:at most|maximum|max|up to|only)\s+(\d+)\s+"
    r"(applies|applications|apply|mails|emails|sends|actions?)",
    re.IGNORECASE,
)
_MIN_SCORE_RE = re.compile(
    r"(?:score|min(?:imum)?\s+score)[^0-9]{0,10}(\d{1,3})", re.IGNORECASE
)
_SITE_RE = re.compile(
    r"(?:on|from|at)\s+([a-z0-9][a-z0-9.-]*\.(?:com|in|org|co|ai|net))\b",
    re.IGNORECASE,
)

_KNOWN_LOCATIONS = (
    "Remote", "Hyderabad", "Bangalore", "Bengaluru", "Chennai",
    "Pune", "Mumbai", "Delhi", "Noida", "Gurugram", "Kolkata",
    "Ahmedabad", "Jaipur", "Indore", "Coimbatore",
)

# Keyword sets for the rule path. A word match scores 1; the
# type with the highest score wins (ties break toward job_hunt,
# the default — a vague message is more likely a job hunt than
# a price watch, and the user corrects the draft anyway).
_TYPE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "price_watch": (
        "price", "watch", "drop", "deal", "discount", "cost",
        "cheaper", "sale", "amazon", "flipkart", "below",
    ),
    "cold_mail": (
        "mail", "email", "cold", "recruiter", "reach out",
        "write to", "contact", "send a mail", "send an email",
    ),
    "job_hunt": (
        "job", "role", "intern", "apply", "hunt", "sde",
        "developer", "engineer", "opening", "hiring", "vacancy",
        "interview",
    ),
}


def _word_count(text: str, word: str) -> int:
    return len(re.findall(rf"\b{re.escape(word)}\b", text))


def _score_types(text: str) -> dict[str, int]:
    lowered = text.lower()
    scores = {}
    for mission_type, words in _TYPE_KEYWORDS.items():
        scores[mission_type] = sum(_word_count(lowered, w) for w in words)
    # A URL plus any price word is decisive for a price watch;
    # an email address plus any mail word is decisive for cold
    # mail. Without the companion signal, a bare URL or address
    # is not enough (a job post URL is not a price watch).
    if _URL_RE.search(text) and scores["price_watch"] > 0:
        scores["price_watch"] += 3
    if _EMAIL_RE.search(text) and scores["cold_mail"] > 0:
        scores["cold_mail"] += 3
    return scores


def _pick_type(text: str) -> tuple[str, dict[str, int]]:
    scores = _score_types(text)
    best = max(scores.values())
    if best == 0:
        return "job_hunt", scores
    for mission_type in ("price_watch", "cold_mail", "job_hunt"):
        if scores[mission_type] == best:
            return mission_type, scores
    return "job_hunt", scores


def _money_value(match) -> int | None:
    if match is None:
        return None
    try:
        value = float(match.group(1).replace(",", ""))
    except ValueError:
        return None
    if match.group(2):
        value *= 1000
    return int(value)


def _extract_caps(text: str, bounds: dict) -> None:
    for match in _CAP_RE.finditer(text):
        count = int(match.group(1))
        kind = match.group(2).lower()
        if kind in ("applies", "applications", "apply"):
            bounds["max_applies_per_day"] = count
        elif kind in ("mails", "emails", "sends"):
            bounds["max_sends_per_day"] = count
        else:
            bounds["max_actions_per_day"] = count


def _extract_locations(text: str) -> list[str]:
    found = []
    for loc in _KNOWN_LOCATIONS:
        if re.search(rf"\b{re.escape(loc)}\b", text, re.IGNORECASE):
            found.append(loc)
    return found


def _draft_name(mission_type: str, config: dict) -> str:
    if mission_type == "job_hunt":
        locations = config.get("locations") or []
        return "Job hunt" + (f" — {locations[0]}" if locations else "")
    if mission_type == "cold_mail":
        recipients = config.get("recipients") or []
        if recipients:
            noun = "recipient" if len(recipients) == 1 else "recipients"
            return f"Cold mail — {len(recipients)} {noun}"
        return "Cold mail"
    url = config.get("url") or ""
    try:
        host = urlparse(url).netloc or ""
    except ValueError:
        host = ""
    return "Price watch" + (f" — {host}" if host else "")


def _rules_draft(message: str) -> dict:
    """The deterministic fallback: keyword type detection +
    regex extraction, local only. Never sent anywhere."""
    mission_type, scores = _pick_type(message)
    config: dict = {}
    bounds: dict = {}
    missing: list[str] = []
    warnings: list[str] = []
    urls = _URL_RE.findall(message)
    url = urls[0].rstrip(".,") if urls else None
    emails = _EMAIL_RE.findall(message)

    if mission_type == "price_watch":
        if url:
            config["url"] = url
        else:
            missing.append("url")
        # A selector cannot be extracted from prose — the user
        # always picks it (in the workspace, or from the page).
        missing.append("selector")
        target = _money_value(_MONEY_RE.search(message))
        if target is None:
            target = _money_value(_TARGET_PRICE_RE.search(message))
        if target is not None:
            config["target_price"] = target
        elif re.search(r"\b(drop|below|cheaper|fall)\b", message, re.IGNORECASE):
            missing.append("target_price")
    elif mission_type == "cold_mail":
        if emails:
            config["recipients"] = sorted(set(emails))
        else:
            missing.append("recipients")
        # Rules cannot know which roles to mail about — the user
        # picks them (from a job hunt, or pasted in).
        missing.append("roles")
        if re.search(r"\b(resume|cv)\b", message, re.IGNORECASE):
            config["attach_resume"] = True
    else:  # job_hunt
        locations = _extract_locations(message)
        if locations:
            config["locations"] = locations
        else:
            missing.append("locations")
        stipend = _money_value(_STIPEND_RE.search(message))
        if stipend is None:
            stipend = _money_value(_STIPEND_AFTER_RE.search(message))
        if stipend is None:
            stipend = _money_value(_MONEY_RE.search(message))
        if stipend is not None:
            config["stipend_min"] = stipend
        else:
            missing.append("stipend_min")
        score_match = _MIN_SCORE_RE.search(message)
        if score_match:
            value = int(score_match.group(1))
            if 0 <= value <= 100:
                config["min_score"] = value
                bounds["min_score"] = value

    _extract_caps(message, bounds)
    sites = sorted({s.lower() for s in _SITE_RE.findall(message)})
    if sites:
        bounds["allowed_sites"] = sites

    # A decisive type signal (score >= 2 and strictly ahead)
    # with nothing missing is high confidence; anything else is
    # low — the user is the judge of a rule-based draft.
    ordered = sorted(scores.values(), reverse=True)
    decisive = ordered[0] >= 2 and (len(ordered) < 2 or ordered[0] > ordered[1])
    confidence = "high" if decisive and not missing else "low"
    if scores[mission_type] == 0:
        warnings.append(
            "Could not tell what kind of mission this is —"
            " guessed job hunt. Pick the type if it is wrong."
        )
    draft_config = _types.validate_config_draft(mission_type, config)
    draft_bounds = _types.validate_bounds(mission_type, bounds)
    draft = {
        "type": mission_type,
        "name": _draft_name(mission_type, draft_config),
        "config": draft_config,
        "bounds": draft_bounds,
    }
    return {
        "draft": draft, "source": "rules", "confidence": confidence,
        "missing_fields": missing, "warnings": warnings,
    }


def _extract_json(raw: str) -> dict:
    """Pull the first {...} object out of planner output
    (fences and prose tolerated). Raises ValueError when there
    is no parseable JSON object."""
    text = (raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in planner output")
    parsed = json.loads(text[start:end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("planner output is not a JSON object")
    return parsed


def _planner_draft(message: str, call_fn) -> dict:
    """The planner path. The message is redacted BEFORE the
    prompt is built — the prompt passed to call_fn carries no
    raw PII. Raises PlannerError / ValueError on failure (the
    caller falls back to rules)."""
    redacted, hits = scan.redact(message)
    if hits:
        logger.info(
            "intake redacted %d PII hits before the planner call",
            len(hits),
        )
    prompt = prompts.build_intake_prompt(redacted)
    raw, _model, _ms = call_fn(prompt)
    parsed = _extract_json(raw)
    mission_type = parsed.get("type")
    if mission_type not in _types.MISSION_TYPES:
        raise ValueError(f"planner returned unknown type {mission_type!r}")
    # Draft validation: required fields may be absent (they
    # become missing_fields); unknown keys / bad values fail.
    config = _types.validate_config_draft(mission_type, parsed.get("config") or {})
    bounds = _types.validate_bounds(mission_type, parsed.get("bounds") or {})
    name = parsed.get("name")
    if not isinstance(name, str) or not name.strip():
        name = _draft_name(mission_type, config)
    name = name.strip()[:120]
    missing: list[str] = []
    for item in parsed.get("missing_fields") or []:
        if isinstance(item, str) and item and item not in missing:
            missing.append(item)
    # Required fields the planner left empty are missing too,
    # whatever the planner claimed.
    spec = _types.MISSION_TYPES[mission_type]
    for field_name, field_spec in spec.config_schema.items():
        if field_spec.get("required") and not config.get(field_name):
            if field_name not in missing:
                missing.append(field_name)
    warnings = []
    if hits:
        warnings.append(
            "Personal details in your message were removed before"
            " drafting — check the draft for anything that needs them."
        )
    confidence = "high" if not missing else "low"
    return {
        "draft": {"type": mission_type, "name": name,
                  "config": config, "bounds": bounds},
        "source": "planner", "confidence": confidence,
        "missing_fields": missing, "warnings": warnings,
    }


def parse_intake(message: str, call_fn=None) -> dict:
    """Parse one chat message into a mission draft.

    Planner first (redacted), rules on any planner failure —
    labelled, never a mock planner, never an exception for a
    planner failure. Raises ValueError only for an empty or
    oversized message (the API maps it to 422)."""
    if not isinstance(message, str) or not message.strip():
        raise ValueError("message must not be empty")
    if len(message) > MAX_MESSAGE_LEN:
        raise ValueError(
            f"message must be at most {MAX_MESSAGE_LEN} characters"
        )
    message = message.strip()
    call_fn = call_fn or router.call_planner
    try:
        return _planner_draft(message, call_fn)
    except Exception as exc:  # noqa: BLE001 - any planner failure falls back to rules
        logger.info("intake planner path failed (%s); using rules", exc)
        result = _rules_draft(message)
        result["warnings"] = [
            "The planner could not draft this mission, so rules"
            " were used instead — check the draft before creating it."
        ] + result["warnings"]
        return result
