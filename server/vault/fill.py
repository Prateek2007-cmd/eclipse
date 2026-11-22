"""The fill map (Sprint 5, plan §6.7): placeholder → real value.

Built by the loop when an action's text carries placeholders.
The map is attached to the execute response — the local channel
to the extension — and the content script turns it into the
executor's fill function. It never goes to the planner, never
enters a log, never leaves localhost.

Two sources:
- Identity placeholders ([NAME_1], [EMAIL_1], [PHONE_1],
  [ADDRESS_1], [ROLL_1]) — from the stored profile (the user
  shared their resume for the job hunt). Filled when a profile
  exists.
- Secret placeholders ([CARD_NUMBER]/[CARD_LAST4], [CVV],
  [PASSWORD_1]) — from the vault: the per-task card (in the
  grant, never stored) and opted-in stored passwords. Filled
  ONLY when the vault is unlocked for the task AND an approval
  grant exists (approval BEFORE vault fill, §5.4). Without both,
  the placeholder is left out and the executor refuses the action
  (it never types a literal token). [CARD_LAST4] fills the full
  card number: it is the placeholder the redactor issues for a
  card number, and the last 4 is only what redacted text shows.

[OTP] is never filled — OTPs are human-only, never stored.

Placeholder↔element binding (§6.9: page text is untrusted data
at BOTH planner and executor): a token is filled only when it
belongs to the target element — the token appears in the target
element's redacted value, or the element's label matches the
token's field (a "Card number" label for [CARD_LAST4], a "CVV"
label for [CVV]). A page whose text contains a literal token
cannot get the card typed into an attacker-controlled field.
The card grant is also scoped to the origin it was unlocked
for (when one was given).
"""
from __future__ import annotations

import json
import re

from ..jobs import profile as profile_mod
from . import grants as grant_store
from .store import Vault

# Secret placeholders and where they come from.
# [CARD_LAST4] is the placeholder the redactor issues for a card
# number (and the one the REDACTION_CONTRACT and the ground truth
# use). When the planner types it into a card field, the vault
# fills the FULL card number — the last 4 is only what redacted
# text shows. [CARD_NUMBER] is accepted as an alias for the same
# fill (a planner may emit either).
CARD_PLACEHOLDERS = {
    "[CARD_NUMBER]": "number",
    "[CARD_LAST4]": "number",
    "[CVV]": "cvv",
}
IDENTITY_PLACEHOLDERS = {
    "[NAME_1]": "name",
    "[EMAIL_1]": "email",
    "[PHONE_1]": "phone",
    "[ADDRESS_1]": "address",
    "[ROLL_1]": "roll",
}
# The label keywords that identify the field a token belongs to
# (matched against the target element's name/text, lowercased).
# The binding is: the token appears in the element's redacted
# value, OR the element's label matches the token's field.
TOKEN_LABELS = {
    "[CARD_NUMBER]": ("card",),
    "[CARD_LAST4]": ("card",),
    "[CVV]": ("cvv", "security code", "cvc"),
    "[PASSWORD_1]": ("password",),
    "[NAME_1]": ("name",),
    "[EMAIL_1]": ("email", "e-mail"),
    "[PHONE_1]": ("phone", "mobile", "tel"),
    "[ADDRESS_1]": ("address",),
    "[ROLL_1]": ("roll",),
}

SECRET_PLACEHOLDERS = frozenset(CARD_PLACEHOLDERS) | {"[PASSWORD_1]"}


def _token_belongs_to_element(token: str, element: dict | None) -> bool:
    """The placeholder↔element binding: the token belongs to the
    target element when it appears in the element's redacted
    value, or the element's label matches the token's field.
    No element → no fill (fail-closed). The label match is on
    WHOLE WORDS: the old substring match filled [PHONE_1] into
    any element whose label contained "tel" — "Hotel
    preferences" got the user's phone number, and "Username"
    got their full name ("name" is a substring of "username")."""
    if element is None:
        return False
    if token in (element.get("value") or ""):
        return True
    label = f"{element.get('name') or ''} {element.get('text') or ''}".lower()
    return any(
        re.search(rf"\b{re.escape(word)}\b", label)
        for word in TOKEN_LABELS.get(token, ())
    )


def build_fill_map(
    conn, task_id: str, placeholders: list[str], origin: str,
    vault: Vault, approval_grants: list[dict] | None = None,
    constraints: str = "", target: dict | None = None,
) -> tuple[dict[str, str], str | None]:
    """Build the fill map for the placeholders an action needs.

    Returns (fill, denied_reason). `denied_reason` is None when
    every requested placeholder was filled, and a short reason
    otherwise (so the UI can show the approval card instead of a
    generic failure).

    `approval_grants` is the list of valid approval grants for
    the task (from the grants table) — secret placeholders need
    one whose detail does not name a draft_id (a mail approval
    never authorises a card fill). `target` is the target element
    dict — a token is filled only when it belongs to that element.
    """
    fill: dict[str, str] = {}
    if not placeholders:
        return fill, None
    # Identity placeholders come from the profile — but only for
    # the job-hunt apply flow (a "role:" constraint) or with an
    # approval grant, and only when the token belongs to the
    # target element.
    identity_allowed = bool(approval_grants) or "role:" in (constraints or "")
    stored = profile_mod.get_profile(conn)
    for token in placeholders:
        if token in IDENTITY_PLACEHOLDERS and stored and identity_allowed:
            if not _token_belongs_to_element(token, target):
                continue
            value = stored.get(IDENTITY_PLACEHOLDERS[token])
            if value:
                fill[token] = str(value)
    # Secret placeholders need the vault unlocked for the task,
    # a vault grant, AND an approval grant (approval BEFORE vault
    # fill). A mail approval (whose detail names a draft_id) never
    # authorises a card fill — the scope is checked here, not only
    # in the caller, so no call site can bypass it.
    secret_needed = [t for t in placeholders if t in SECRET_PLACEHOLDERS]
    if not secret_needed:
        return fill, None
    if not vault.unlocked:
        return fill, "vault is locked (unlock it first)"
    grant = grant_store.GRANTS.get(task_id)
    if grant is None:
        return fill, "no vault grant for this task (unlock the vault for it)"
    # Origin scoping is fail-closed: a grant scoped to an origin
    # fills ONLY there. An empty/unparseable origin cannot prove
    # it is the grant's origin, so it is denied too (the old
    # check skipped the comparison when the origin was empty).
    if grant.origin and grant.origin != origin:
        return fill, (
            f"the card grant is for {grant.origin}, "
            f"not {origin or 'an unknown origin'}"
        )
    task_approvals = []
    for approval in approval_grants or []:
        try:
            detail = json.loads(approval.get("detail") or "{}")
        except (json.JSONDecodeError, TypeError, AttributeError):
            detail = {}
        if "draft_id" not in detail:
            task_approvals.append(approval)
    if not task_approvals:
        return fill, "approval needed before the vault can fill (approve the payment)"
    grant.touch()
    for token in secret_needed:
        if not _token_belongs_to_element(token, target):
            continue
        if token in CARD_PLACEHOLDERS:
            if grant.card is None:
                continue
            value = grant.card.get(CARD_PLACEHOLDERS[token])
            if value:
                fill[token] = str(value)
        elif token == "[PASSWORD_1]":
            value = vault.get_password(origin, "password")
            if value:
                fill[token] = value
    missing = [t for t in secret_needed if t not in fill]
    if missing:
        return fill, f"could not fill {', '.join(missing)} for this field"
    return fill, None
