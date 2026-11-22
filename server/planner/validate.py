"""Planner-output validation (Sprint 3, plan §6.2/§6.3).

Every planner action must pass, before anything executes:
- valid JSON (a ```json fence is tolerated and stripped)
- action in the allow-list
- element actions name an element_id present in the current
  observation, and that element is visible + interactable
- "type" text is non-empty; "navigate" targets an allowed site
- injection markers in the action text are rejected and logged
  (page text is data, never instructions — §6.9)
A failed validation is a planner retry (max 3 planner calls/step),
never an execution.
"""
from __future__ import annotations

import json
import logging
import re
from urllib.parse import urlparse

from .prompts import INJECTION_MARKERS

logger = logging.getLogger("eclipse.planner")

ACTION_TYPES = ("click", "type", "select", "navigate", "wait", "scroll")
# Actions that need an interactable target. Scroll needs the element
# to exist and be visible, but scrollable regions are typically NOT
# marked interactable by the walker — so it is checked separately.
INTERACTABLE_ACTIONS = ("click", "type", "select")
# Roles a "type" action may target (a textbox-like field). Typing
# into a button passes the old check and fails cryptically in the
# executor; reject it here.
TYPEABLE_ROLES = ("textbox", "searchbox", "spinbutton", "combobox")


class ValidationError(ValueError):
    """The planner output failed validation; retry or ask the user."""


def _strip_fence(text: str) -> str:
    text = text.strip()
    m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    return m.group(1) if m else text


def parse_action(raw: str) -> dict:
    """Parse the planner's raw text into an action dict."""
    try:
        data = json.loads(_strip_fence(raw))
    except json.JSONDecodeError as exc:
        raise ValidationError(f"planner output is not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValidationError("planner output must be a JSON object")
    return data


def validate_action(
    action: dict,
    element_ids: set[str],
    interactable_ids: set[str],
    allowed_sites: list[str] | None = None,
    elements: list[dict] | None = None,
    current_url: str | None = None,
) -> dict:
    """Validate one planner action against the current observation.

    Returns the action unchanged when valid. Raises ValidationError
    otherwise. `allowed_sites` is the task's site allow-list for
    navigate; with no allow-list, only the current site (from
    `current_url`) is permitted — the ACTION_SCHEMA already
    promises "the allowed-sites list (or the current site)", and
    without this a page that talks the planner into navigating
    elsewhere (injection via page text) would succeed. When
    neither is known, any http(s) URL is accepted (the old
    behaviour — callers that know the current page must pass it).
    `elements` (the full observation list) enables the role check
    for "type" and the visibility check for "scroll".
    """
    if not isinstance(action, dict):
        raise ValidationError("planner output must be a JSON object")
    act = action.get("action")
    if act not in ACTION_TYPES:
        raise ValidationError(f"unknown action: {act!r}")
    by_id: dict[str, dict] = {}
    if elements is not None:
        by_id = {e["id"]: e for e in elements if isinstance(e.get("id"), str)}
    if act in INTERACTABLE_ACTIONS or act == "scroll":
        el = action.get("element_id")
        if not isinstance(el, str) or not el:
            raise ValidationError(f"{act} needs an element_id (a string)")
        if el not in element_ids:
            raise ValidationError(f"element_id {el!r} is not in the current observation")
        if act in INTERACTABLE_ACTIONS and el not in interactable_ids:
            raise ValidationError(f"element {el!r} is not interactable")
        if act == "scroll" and elements is not None:
            target = by_id.get(el)
            if target is not None and target.get("visible") is False:
                raise ValidationError(f"element {el!r} is not visible")
        if act == "type" and elements is not None:
            target = by_id.get(el)
            if target is not None:
                role = target.get("role")
                tag = str(target.get("tag", "")).lower()
                if role not in TYPEABLE_ROLES and tag not in ("input", "textarea"):
                    raise ValidationError(
                        f"cannot type into {el!r} (role {role!r}, tag {tag!r})"
                    )
    if act == "type":
        text = action.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValidationError("type needs non-empty text (a string)")
        _check_injection(text)
    if act == "select":
        text = action.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValidationError("select needs a text (option) value (a string)")
        _check_injection(text)
    if act == "navigate":
        url = action.get("url")
        if not isinstance(url, str) or not url.lower().startswith(("http://", "https://")):
            raise ValidationError("navigate needs a full http(s) URL")
        # hostname excludes the port (urlparse().netloc does not).
        host = (urlparse(url).hostname or "").lower()
        if allowed_sites:
            if not any(host == s.lower() or host.endswith("." + s.lower()) for s in allowed_sites):
                raise ValidationError(f"navigate to {host} is outside the allowed sites")
        elif current_url:
            current_host = (urlparse(current_url).hostname or "").lower()
            if current_host and not (
                host == current_host or host.endswith("." + current_host)
            ):
                raise ValidationError(
                    f"navigate to {host} is outside the current site ({current_host})"
                )
    if act == "wait":
        secs = action.get("seconds")
        if isinstance(secs, bool) or not isinstance(secs, (int, float)) or not 1 <= secs <= 30:
            raise ValidationError("wait needs seconds between 1 and 30")
    if isinstance(action.get("reason"), str):
        _check_injection(action["reason"])
    return action


def _check_injection(text: str) -> None:
    lowered = text.lower()
    for marker in INJECTION_MARKERS:
        if marker in lowered:
            logger.warning("injection marker in planner output: %r", marker)
            raise ValidationError(f"planner output contains an injection marker: {marker!r}")


def parse_plan(raw: str) -> list[dict]:
    """Parse the planner's plan output into a list of step dicts."""
    try:
        data = json.loads(_strip_fence(raw))
    except json.JSONDecodeError as exc:
        raise ValidationError(f"plan output is not JSON: {exc}") from exc
    if not isinstance(data, list) or not data:
        raise ValidationError("plan output must be a non-empty JSON list")
    steps: list[dict] = []
    for i, item in enumerate(data, start=1):
        if not isinstance(item, dict) or not item.get("description"):
            raise ValidationError(f"plan step {i} needs a description")
        steps.append(
            {
                "step": item.get("step", i),
                "description": str(item["description"]),
                "expected_end_state": str(item.get("expected_end_state", "")),
            }
        )
    if len(steps) > 25:
        raise ValidationError("plan exceeds the 25-step task limit")
    return steps
