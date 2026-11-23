"""Chat classification (PR #6): pure decision logic for the Agent
view's chat thread. No DB, no network, no planner calls — every
function here is unit-testable on its own.

The bug this fixes (Prateek's live test): typing "hi" twice created
two dead tasks stuck at "created". A greeting must NEVER create a
task, so the rules are authoritative for small talk: greetings,
thanks, goodbyes and capability questions are answered locally and
never reach the planner (chat_api.respond returns them before any
planner call).

Decision order in classify_rules:
  1. greeting / small talk  -> kind "chat" (hard: never a task)
  2. imperative + object    -> kind "task_created"
  3. anything else          -> kind "chat" + ONE clarifying question

Steering (classify_steering) runs BEFORE this decision, but only
when the conversation has an active task: stop/cancel/pause act on
that task, and a refinement narrows it — neither creates a task.
A clear new-task instruction is never steering: it starts a new
task instead.
"""
from __future__ import annotations

import json
import re

MAX_MESSAGE_LEN = 2000

# ----------------------------------------------------------------
# Normalisation
# ----------------------------------------------------------------

_GREETING_RE = re.compile(
    r"^(good morning|good afternoon|good evening|good day|hello|hiya"
    r"|howdy|hey|hi|yo|namaste)\b[,.!?\s]*(.*)$"
)
_GREETING_FILLER = {"", "there", "eclipse", "again", "there eclipse"}


def _clean(text: str) -> str:
    """Lowercased, whitespace-collapsed, punctuation-trimmed."""
    return re.sub(r"\s+", " ", text.strip().lower()).strip(" .!?,")


def _strip_prefix(text: str, prefixes: tuple[str, ...]) -> str:
    """Strip leading prefixes (matched case-insensitively) from
    the ORIGINAL text, preserving its case. Loops: "please, can
    you find…" strips both."""
    stripped = text.strip()
    changed = True
    while changed:
        changed = False
        lowered = stripped.lower()
        for prefix in prefixes:
            if lowered.startswith(prefix):
                stripped = stripped[len(prefix):].strip()
                changed = True
                break
    return stripped


_PREFIXES = (
    "please ",
    "can you ", "could you ", "will you ", "would you ",
    "i want you to ", "i need you to ", "i'd like you to ",
    "help me to ", "help me ",
    "i want to ", "i want ", "i need to ", "i need ",
    "i'd like to ", "i'd like ",
    "i am ", "i'm ",
)

# ----------------------------------------------------------------
# Small talk (hard chat — never a task, never the planner)
# ----------------------------------------------------------------

_CAPABILITY = (
    "I can search for job roles and apply to matching ones, watch a"
    " price on a page, and draft cold mails — and I always ask"
    " before applying, sending, or spending anything. Just tell me"
    " what you need."
)
_WHO_AM_I = (
    "I'm Eclipse, your browser agent — I do web tasks for you and"
    " ask before anything that applies, sends, or spends."
)


def _greeting_reply(greeting: str) -> str:
    # Sentence case, not title case: "Good morning!", not
    # "Good Morning!".
    opener = greeting[0].upper() + greeting[1:] if greeting else greeting
    return (
        f"{opener}! I'm Eclipse — I can find and apply to roles,"
        " watch prices, or draft cold mails for you. What would you"
        " like me to do?"
    )


def _small_talk(text: str) -> tuple[str, str] | None:
    """(reason, reply) when the message is small talk, else None.

    `text` is the cleaned (lowercased) message. Every match is
    anchored: "hi, find me jobs" is NOT a greeting — the remainder
    after the greeting carries an instruction, so it falls through
    to the task check.
    """
    # Greeting only (plus optional filler): "hi", "good morning",
    # "hi there". A greeting followed by anything else is not a
    # greeting — "hi, find me jobs" is a task.
    match = _GREETING_RE.match(text)
    if match:
        remainder = match.group(2).strip(" .!?,")
        if remainder in _GREETING_FILLER:
            return ("greeting", _greeting_reply(match.group(1)))
    # "how are you" / "how's it going" / "what's up" — only when
    # the message carries no instruction with it.
    if re.search(
        r"\bhow are you\b|\bhow'?s it going\b|\bwhat'?s up\b"
        r"|\bhow are u\b",
        text,
    ) and not _has_imperative(text):
        return (
            "how_are_you",
            "I'm doing well, thanks — ready when you are. Want me"
            " to find roles, watch a price, or draft a mail?",
        )
    # Thanks, on its own: "thanks", "thank you so much".
    thanks = re.match(
        r"^(thanks|thank you|thx|ty)\b[,.!?\s]*(.*)$", text
    )
    if thanks:
        remainder = thanks.group(2).strip(" .!?,")
        if remainder in ("", "a lot", "so much", "very much", "eclipse",
                         "for the help", "for your help", "a bunch"):
            return (
                "thanks",
                "You're welcome — happy to help. Anything else"
                " you'd like me to do?",
            )
    # Goodbye, on its own.
    bye = re.match(
        r"^(bye|goodbye|good night|goodnight|see you|see ya)\b"
        r"[,.!?\s]*(.*)$",
        text,
    )
    if bye:
        remainder = bye.group(2).strip(" .!?,")
        if remainder in ("", "eclipse", "for now"):
            return ("bye", "Goodbye! I'll be here whenever you need me.")
    # Capabilities / identity — only when the message is the
    # question, not a question plus an instruction.
    if not _has_imperative(text):
        if re.search(
            r"what can you do|what do you do|what can u do"
            r"|how do you work|what are you for",
            text,
        ):
            return ("capabilities", _CAPABILITY)
        if re.search(r"who are you|what are you\b", text):
            return ("capabilities", _WHO_AM_I)
        if re.match(r"^help(\s+me)?[.!?\s]*$", text) or re.match(
            r"^i need help[.!?\s]*$", text
        ):
            return ("capabilities", _CAPABILITY)
    return None


# ----------------------------------------------------------------
# Task instructions (imperative verb + object)
# ----------------------------------------------------------------

_TASK_VERBS = (
    "find", "search", "apply", "watch", "track", "monitor",
    "draft", "write", "send", "check", "open", "book",
    "remind", "get", "fetch", "list", "create", "start",
)
_TASK_VERB_PHRASES = ("look for", "looking for")
# Words after the verb that do not count as an object: "find it"
# has no object, so it is ambiguous, not a task.
_OBJECT_STOPWORDS = {
    "me", "for", "a", "an", "the", "some", "any", "my", "to",
    "on", "in", "at", "up", "out", "it", "this", "that", "them",
    "us", "please", "out",
}
_CONSEQUENTIAL_WORDS = (
    "apply", "send", "buy", "pay", "book", "order", "subscribe",
    "checkout", "purchase",
)


def _verb_and_object(text: str) -> tuple[str, str] | None:
    """(verb, object-text) when the message starts with an
    imperative verb after its greeting/polite prefixes, else None.
    "hi, find me jobs" counts — the greeting is stripped first."""
    core = text
    greeting = _GREETING_RE.match(core)
    if greeting and greeting.group(2).strip():
        core = greeting.group(2).strip()
    core = _strip_prefix(core, _PREFIXES)
    for phrase in _TASK_VERB_PHRASES:
        if core.startswith(phrase + " ") or core == phrase:
            return (phrase, core[len(phrase):].strip())
    first = core.split(" ", 1)
    if first[0] in _TASK_VERBS:
        rest = first[1] if len(first) > 1 else ""
        return (first[0], rest.strip())
    return None


def _has_object(object_text: str) -> bool:
    tokens = re.findall(r"[a-z0-9]+", object_text.lower())
    return any(t not in _OBJECT_STOPWORDS for t in tokens)


def _has_imperative(text: str) -> bool:
    """Does the message carry an imperative verb + object? Used to
    keep small talk and steering from swallowing instructions."""
    found = _verb_and_object(text)
    return found is not None and _has_object(found[1])


def _is_task_instruction(text: str) -> bool:
    return _has_imperative(text)


def _task_reply(text: str) -> str:
    """Confirm a task in plain words, from the user's own message.
    "Find me remote SDE intern roles in Hyderabad" becomes
    "On it — I'll find remote SDE intern roles in Hyderabad."
    Consequential goals get the approval note: Eclipse always asks
    before applying, sending, or spending."""
    cleaned = text.strip()
    greeting = _GREETING_RE.match(cleaned.lower())
    if greeting and greeting.group(2).strip():
        # Slice the original by the greeting's length, keeping case.
        cleaned = cleaned[len(greeting.group(1)):].lstrip(" ,.!?").strip()
    cleaned = _strip_prefix(cleaned, _PREFIXES)
    # "find me X" reads better as "find X".
    for verb in ("find", "search", "get"):
        if cleaned.lower().startswith(f"{verb} me "):
            cleaned = cleaned[len(verb) + 4:].strip()
            cleaned = f"{verb} {cleaned}"
            break
    sentence = cleaned[0].lower() + cleaned[1:] if cleaned else cleaned
    sentence = sentence.rstrip(".!") + "."
    reply = f"On it — I'll {sentence}"
    lowered = text.lower()
    if "apply" in lowered or "application" in lowered:
        reply += " I'll ask before each apply."
    elif "send" in lowered or "mail" in lowered:
        reply += " I'll ask before sending anything."
    elif any(w in lowered for w in _CONSEQUENTIAL_WORDS):
        reply += " I'll ask before anything that applies, sends, or spends."
    return reply


# ----------------------------------------------------------------
# Clarification (ambiguous fragments — ONE question, never a task)
# ----------------------------------------------------------------

_JOB_WORDS = (
    "intern", "job", "role", "sde", "hiring", "career", "vacancy",
    "opening", "engineer", "developer", "fresher",
)
_PRICE_WORDS = ("price", "deal", "discount", "cheap", "cost", "sale", "offer")
_MAIL_WORDS = ("mail", "email", "recruiter", "outreach", "cover letter")


def _clarification(text: str) -> str:
    """One clarifying question, tailored to the fragment's topic.
    "internships" asks about intern roles; anything else gets the
    generic question plus an example of a message that works."""
    lowered = text.lower()
    if any(re.search(rf"\b{w}", lowered) for w in _JOB_WORDS):
        return (
            "Are you looking for intern roles? Tell me a bit more —"
            " remote or a city, and any stipend floor?"
        )
    if any(re.search(rf"\b{w}\b", lowered) for w in _PRICE_WORDS):
        return (
            "Want me to watch a price? Send me the page link and"
            " the price to tell you about."
        )
    if any(re.search(rf"\b{w}\b", lowered) for w in _MAIL_WORDS):
        return (
            "Want me to draft a cold mail? Tell me who it's for"
            " and what role it's about."
        )
    return (
        "Tell me a bit more — what should I do with that? For"
        " example: 'find remote SDE intern roles in Hyderabad'."
    )


def classify_rules(text: str) -> dict:
    """The rules decision for one message. Pure.

    Returns {"kind", "reply_text", "reason", "hard_chat"}:
    - kind "chat" + hard_chat True: small talk. The caller must
      never send this to the planner and never create a task.
    - kind "task_created": a clear instruction (imperative + an
      object). The goal is the message itself.
    - kind "chat" + reason "ambiguous": a fragment the rules
      cannot act on. The caller may still ask the planner, which
      has the conversation history to resolve it — but with no
      history the rules win (see chat_api.respond).
    """
    cleaned = _clean(text)
    small = _small_talk(cleaned)
    if small is not None:
        return {
            "kind": "chat", "reply_text": small[1],
            "reason": small[0], "hard_chat": True,
        }
    if _is_task_instruction(cleaned):
        return {
            "kind": "task_created", "reply_text": _task_reply(text),
            "reason": "task", "hard_chat": False,
        }
    return {
        "kind": "chat", "reply_text": _clarification(cleaned),
        "reason": "ambiguous", "hard_chat": False,
    }


# ----------------------------------------------------------------
# Steering (only when a task is active — see chat_api.respond)
# ----------------------------------------------------------------

_STOP_RES = (
    re.compile(
        r"^(please\s+)?(stop|cancel|halt|abort)"
        r"(\s+(it|this|that|the(\s+\w+){1,2}|everything|all))?"
        r"(\s+please)?[.!?]*$"
    ),
    re.compile(
        r"^(never\s+mind|nevermind|forget\s+it|forget\s+about\s+it"
        r"|call\s+it\s+off|that'?s\s+enough)[.!?]*$"
    ),
)
_PAUSE_RES = (
    re.compile(
        r"^(please\s+)?pause"
        r"(\s+(it|this|that|the(\s+\w+){1,2}))?"
        r"(\s+please)?[.!?]*$"
    ),
    re.compile(r"^hold\s+on[.!?]*$"),
)
# A bare constraint word, on its own, narrows the active task:
# "remote" means remote only; "Hyderabad" means Hyderabad only.
_BARE_CONSTRAINT_WORDS = {
    "remote", "hybrid", "onsite", "on-site", "wfh",
    "hyderabad", "bangalore", "bengaluru", "chennai", "pune",
    "mumbai", "delhi", "noida", "gurugram", "gurgaon", "kolkata",
    "ahmedabad", "jaipur", "indore", "coimbatore",
}
_CITY_WORDS = (
    "hyderabad", "bangalore", "bengaluru", "chennai", "pune",
    "mumbai", "delhi", "noida", "gurugram", "gurgaon", "kolkata",
    "ahmedabad", "jaipur", "indore", "coimbatore",
)


def _is_refinement(text: str) -> bool:
    """Is this message a constraint change for the active task
    ("remote only", "min 15k stipend", "not that company")?
    Only called after the new-task check, so an instruction like
    "find roles with min 15k stipend" never lands here."""
    if re.search(r"\bonly\b", text):
        return True
    if re.search(
        r"\b(min|minimum|at least|at most|max|maximum|under|below"
        r"|over|above)\b",
        text,
    ):
        return True
    # "no" / "not" need a word after them: a bare "no" is an
    # answer to a question, not a refinement.
    if re.search(
        r"^not\s+\w|\bnot that\b|^don'?t\b|^do not\b|\bexclude\b"
        r"|\bskip\b|\bavoid\b|^no\s+\w",
        text,
    ):
        return True
    if re.search(r"\binstead\b", text):
        return True
    if re.search(r"^change\b|^make it\b|^also\b", text):
        return True
    words = text.split()
    if len(words) == 1 and words[0] in _BARE_CONSTRAINT_WORDS:
        return True
    # A short message naming a place or a work mode narrows the
    # hunt: "in Hyderabad", "remote roles".
    if len(words) <= 4 and (
        re.search(r"\b(remote|hybrid|onsite|wfh)\b", text)
        or any(re.search(rf"\b{city}\b", text) for city in _CITY_WORDS)
    ):
        return True
    return False


def classify_steering(text: str) -> str | None:
    """Steering intent for a message arriving while a task is
    active. Returns "cancel" | "pause" | "refine" | None. Pure.

    A clear new-task instruction is NEVER steering — it starts a
    new task instead (checked first). A leading "no," / "actually"
    is stripped before the checks, so "no, remote only" refines
    and "no, cancel it" cancels.
    """
    cleaned = _clean(text)
    core = re.sub(r"^(no,|actually|well)[,.]?\s+", "", cleaned)
    if _is_task_instruction(core):
        return None
    if any(rx.match(core) for rx in _STOP_RES):
        return "cancel"
    if any(rx.match(core) for rx in _PAUSE_RES):
        return "pause"
    if _is_refinement(core):
        return "refine"
    return None


def refinement_phrase(text: str) -> str:
    """The refinement in the user's own words, trimmed for the
    confirmation reply: "No, remote only" -> "remote only"."""
    cleaned = text.strip().strip(".!")
    lowered = cleaned.lower()
    for prefix in ("no, ", "no ", "actually ", "please "):
        if lowered.startswith(prefix):
            cleaned = cleaned[len(prefix):].strip()
            lowered = cleaned.lower()
            break
    if cleaned:
        cleaned = cleaned[0].lower() + cleaned[1:]
    return cleaned


def task_label(goal: str) -> str:
    """A short name for a task, for steering replies: "the job
    hunt", "the price watch", "the mail run", else "the task"."""
    lowered = (goal or "").lower()
    if any(w in lowered for w in ("intern", "job", "role", "hiring")):
        return "the job hunt"
    if any(w in lowered for w in ("price", "watch")):
        return "the price watch"
    if any(w in lowered for w in ("mail", "email")):
        return "the mail run"
    return "the task"


# ----------------------------------------------------------------
# Planner path (prompt + output parsing — still pure text work)
# ----------------------------------------------------------------

_CHAT_PROMPT_INSTRUCTIONS = """\
You are Eclipse, a privacy-first browser agent. Classify the user's message and draft a reply. Return ONE JSON object, nothing else (no prose, no markdown fence):
{"kind": "chat" | "task_created", "reply_text": "<your reply>", "constraints": "<task limits or null>"}

Rules:
- kind "chat" for greetings, small talk, thanks, goodbyes, questions about what you can do, and messages too vague to act on. A bare noun like "internships" is too vague — ask ONE clarifying question instead of creating a task.
- kind "task_created" ONLY when the message is a clear instruction to do something in the browser (find roles, watch a price, draft a mail, check a page). The goal is the message itself. A short reply to your clarifying question ("remote ones", "yes, Hyderabad") CAN be a task — resolve it against the conversation above.
- NEVER return task_created for a greeting or small talk.
- reply_text is plain words, one or two sentences. For a task, confirm what you will do and that you will ask before anything that applies, sends, or spends.
- constraints: only real limits the message states (remote only, a city, a stipend floor), or null. Never invent one.
- Only use what the message and the conversation actually say. Never invent a site, a role, a URL, or a price."""


def build_chat_prompt(
    text: str, history: list[dict], facts: list[str]
) -> str:
    """The planner prompt: instructions + the last few chat
    messages + recalled memory facts + the new message. `history`
    is oldest-first [{"role", "text"}]; `facts` are plain strings.
    The caller redacts the assembled prompt before the call (the
    loop.plan_task pattern)."""
    parts = [_CHAT_PROMPT_INSTRUCTIONS]
    if history:
        lines = [f"{m['role']}: {m['text']}" for m in history]
        parts.append("CONVERSATION SO FAR:\n" + "\n".join(lines))
    if facts:
        parts.append("REMEMBERED FACTS:\n" + "\n".join(facts))
    parts.append(f"MESSAGE: {text}")
    return "\n\n".join(parts)


def _extract_json(raw: str) -> dict:
    """Pull the first {...} object out of planner output (fences
    and prose tolerated). Raises ValueError when there is no
    parseable JSON object. (Same shape as the intake's — the
    planner is a text model, not a JSON API.)"""
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


def parse_planner_reply(raw: str) -> dict:
    """Validate one planner reply. Returns {"kind", "reply_text",
    "constraints"}; raises ValueError on anything unusable (the
    caller falls back to rules)."""
    parsed = _extract_json(raw)
    kind = parsed.get("kind")
    if kind not in ("chat", "task_created"):
        raise ValueError(f"planner returned unknown kind {kind!r}")
    reply = parsed.get("reply_text")
    if not isinstance(reply, str) or not reply.strip():
        raise ValueError("planner returned no reply_text")
    constraints = parsed.get("constraints")
    if constraints is not None and not isinstance(constraints, str):
        raise ValueError("planner constraints are not a string")
    return {
        "kind": kind,
        "reply_text": reply.strip()[:1000],
        "constraints": (constraints or "").strip() or None,
    }
