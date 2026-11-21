"""Context budget (Sprint 3, plan §6.5): planner input max 8K tokens.

Token estimate is len(text) // 4 (rough, but consistent). Only
visible interactive elements are sent; the list is truncated from
the least useful end until the prompt fits. Routine steps aim for
under 2K tokens.
"""
from __future__ import annotations

MAX_TOKENS = 8000
ROUTINE_TARGET_TOKENS = 2000
CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN)


def format_observation(url: str, elements: list[dict]) -> str:
    """Compact redacted observation: URL + visible interactive elements.

    Each element is one line: id, tag, role, name/text, value
    (placeholders only — the redactor already replaced real values).
    Non-interactive, invisible elements are dropped: the planner
    cannot act on them.
    """
    lines = [f"URL: {url}"]
    for el in elements:
        if not el.get("visible", True):
            continue
        if not el.get("interactable", False):
            continue
        parts = [f"[{el.get('id')}]", str(el.get("tag", "")).lower()]
        role = el.get("role")
        if role:
            parts.append(f"role={role}")
        name = (el.get("name") or "").strip()
        text = (el.get("text") or "").strip()
        shown = name or text
        if shown:
            parts.append(shown[:80])
        value = el.get("value")
        if value:
            parts.append(f"value={str(value)[:40]}")
        lines.append(" ".join(p for p in parts if p))
    return "\n".join(lines)


def fit_to_budget(
    fixed_parts: list[str],
    observation_text: str,
    max_tokens: int = MAX_TOKENS,
) -> str:
    """Assemble the prompt, truncating the observation to fit.

    Fixed parts (goal, contract, checkpoint) are never cut. The
    observation is truncated line by line from the end until the
    whole prompt fits the budget.
    """
    fixed = "\n\n".join(p for p in fixed_parts if p)
    fixed_tokens = estimate_tokens(fixed)
    if fixed_tokens >= max_tokens:
        # Even the fixed parts overflow: return them alone (the
        # planner still gets the goal and contract; the caller logs
        # the overflow).
        return fixed
    budget_chars = (max_tokens - fixed_tokens) * CHARS_PER_TOKEN
    if len(observation_text) <= budget_chars:
        return fixed + "\n\n" + observation_text
    # Truncate by whole lines so no element is half-described.
    # Drop from the FRONT of the list (DOM order): the submit button
    # and the action targets usually sit at the end, and losing them
    # first is the worst truncation. Reserve room for the omission
    # note itself, so the result stays within budget (the note is
    # ~50 chars; reserve 80).
    budget_chars -= 80
    lines = observation_text.splitlines()
    # Keep the last lines that fit the budget.
    kept: list[str] = []
    used = 0
    for line in reversed(lines):
        if used + len(line) + 1 > budget_chars:
            break
        kept.append(line)
        used += len(line) + 1
    kept.reverse()
    if len(kept) < len(lines):
        kept.insert(0, f"... [{len(lines) - len(kept)} earlier elements omitted for the 8K budget]")
    return fixed + "\n\n" + "\n".join(kept)
