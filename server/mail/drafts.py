"""Cold-mail drafts (Sprint 5, plan Flow 2).

A draft is one short personal mail per recruiter: the role, why
this candidate fits (the match reasons), and the resume attached.
The draft is a deterministic template — the planner can refine
it, but the draft itself is built from the role + profile +
match reasons, so it is reproducible and testable.

Dedup (§6.9, Flow 2): no duplicate mails to the same recruiter
for the same role. Before a draft is created, the drafts table
and the mail log are checked for (recipient, role_id); an
existing draft or a sent mail blocks a new one.
"""
from __future__ import annotations

import sqlite3
import time
import uuid


def draft_subject(role: dict) -> str:
    title = role.get("title", "the role")
    company = role.get("company", "")
    return f"Application for {title}" + (f" at {company}" if company else "")


def _ordinal(year: int) -> str:
    """1st, 2nd, 3rd, 4th (not "2th")."""
    if 10 <= year % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(year % 10, "th")
    return f"{year}{suffix}"


def draft_body(role: dict, profile: dict, reasons: list[str]) -> str:
    """The mail body. Short, personal, specific: the role, why
    this candidate fits. No attachment is promised — the resume
    attachment lands with the Gmail send in Sprint 7; until
    then the mail must not claim one is attached."""
    name = profile.get("name") or "I"
    degree = profile.get("degree") or ""
    year = profile.get("year")
    cgpa = profile.get("cgpa")
    skills = ", ".join(profile.get("skills") or [])
    title = role.get("title", "the role")
    company = role.get("company", "your company")
    stipend = role.get("stipend")
    lines = [
        "Hi,",
        "",
        f"I'm {name}, a {_ordinal(year)}-year {degree} student"
        f" (CGPA {cgpa})" if year and cgpa else f"I'm {name}.",
        f"I'm writing about the {title} role at {company}.",
        "",
    ]
    if reasons:
        lines.append("Why I fit:")
        for reason in reasons:
            lines.append(f"- {reason}")
        lines.append("")
    if skills:
        lines.append(f"My skills: {skills}.")
        lines.append("")
    if stipend:
        lines.append(f"I'm looking for a role with a stipend of ₹{stipend:,}/month or above.")
        lines.append("")
    lines.extend([
        "I'd love a short call to discuss the role — and I can share my resume.",
        "",
        "Thanks,",
        name,
    ])
    return "\n".join(lines)


def already_mailed(conn, recipient: str, role_id: str) -> bool:
    """True when a live draft already exists for (recipient,
    role_id), a mail was already sent for it, or a mail to this
    address BOUNCED for it. Plan §7.1: a bounce stops further
    sends to that address — the mailbox does not exist, so
    retrying the same address is pointless. A corrected address
    is a different recipient and is not blocked. A no-reply
    draft does NOT block a retry — the recruiter may simply not
    have answered yet."""
    recipient = recipient.strip().lower()
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM mail_drafts"
        " WHERE LOWER(recipient) = ? AND role_id = ?"
        " AND status IN ('draft', 'approved', 'sent')",
        (recipient, role_id),
    ).fetchone()
    if row["c"]:
        return True
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM mail_log"
        " WHERE LOWER(recipient) = ? AND role_id = ? AND status = 'sent'",
        (recipient, role_id),
    ).fetchone()
    if row["c"]:
        return True
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM mail_log"
        " WHERE LOWER(recipient) = ? AND status = 'bounced'",
        (recipient,),
    ).fetchone()
    return bool(row["c"])


def create_draft(conn, task_id: str, recipient: str, role: dict,
                 profile: dict, reasons: list[str]) -> str | None:
    """Create a draft for (recipient, role). Returns the draft id,
    or None when a draft/mail already exists for this pair
    (dedup). The unique index on (LOWER(recipient), role_id) is
    the real dedup — the check above is only the fast path; a
    concurrent insert raises IntegrityError and returns None."""
    role_id = role.get("id") or role.get("url")
    if not role_id:
        raise ValueError("role has no id (dedup needs one)")
    if already_mailed(conn, recipient, role_id):
        return None
    draft_id = uuid.uuid4().hex[:12]
    now = int(time.time())
    try:
        conn.execute(
            "INSERT INTO mail_drafts (id, task_id, recipient, role_id,"
            " subject, body, status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, 'draft', ?, ?)",
            (draft_id, task_id, recipient, role_id,
             draft_subject(role), draft_body(role, profile, reasons), now, now),
        )
    except sqlite3.IntegrityError:
        conn.rollback()
        return None
    conn.commit()
    return draft_id


def get_draft(conn, draft_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM mail_drafts WHERE id = ?", (draft_id,)
    ).fetchone()
    return dict(row) if row else None


def list_drafts(conn, task_id: str | None = None, status: str | None = None) -> list[dict]:
    query = "SELECT * FROM mail_drafts"
    params: list = []
    clauses = []
    if task_id:
        clauses.append("task_id = ?")
        params.append(task_id)
    if status:
        clauses.append("status = ?")
        params.append(status)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY created_at ASC"
    return [dict(r) for r in conn.execute(query, params).fetchall()]


def approve_draft(conn, draft_id: str) -> bool:
    """Approve a draft (the user read it and said yes). Returns
    False when the draft does not exist or is not a draft."""
    now = int(time.time())
    cur = conn.execute(
        "UPDATE mail_drafts SET status = 'approved', updated_at = ?"
        " WHERE id = ? AND status = 'draft'",
        (now, draft_id),
    )
    conn.commit()
    return cur.rowcount == 1


def update_draft_body(conn, draft_id: str, body: str) -> bool:
    """The user edits a draft before approving it."""
    now = int(time.time())
    cur = conn.execute(
        "UPDATE mail_drafts SET body = ?, updated_at = ?"
        " WHERE id = ? AND status IN ('draft', 'approved')",
        (body, now, draft_id),
    )
    conn.commit()
    return cur.rowcount == 1
