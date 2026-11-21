"""Cold-mail send + log (Sprint 5, plan Flow 2).

Send only after approval: a draft must be 'approved' and the
task must hold a valid approval grant (the same grant machinery
as Sprint 4 — approval BEFORE the send, §5.4). The send is
recorded in mail_log with the outcome per recruiter: sent,
bounced, or no_reply.

The transport is pluggable. The mock transport (used by tests)
decides by the recipient address: an address containing
"bounce" bounces, one containing "noreply" never replies, and
anything else is sent. Sprint 7 replaces it with the Gmail
connector, which is when real delivery and bounce detection
land — the draft/approve/dedup/log machinery does not change.
"""
from __future__ import annotations

import time
import uuid

from . import drafts as draft_mod


class SendResult:
    def __init__(self, status: str, detail: str = ""):
        self.status = status  # sent | bounced | no_reply
        self.detail = detail


def mock_transport(recipient: str, subject: str, body: str) -> SendResult:
    """The test transport: decides the outcome from the address."""
    lowered = recipient.lower()
    if "bounce" in lowered:
        return SendResult("bounced", "550 mailbox unavailable (mock)")
    if "noreply" in lowered:
        return SendResult("no_reply", "no reply within 7 days (mock)")
    return SendResult("sent", "250 OK (mock)")


def send_draft(conn, draft_id: str, transport=None) -> SendResult:
    """Send an approved draft. Records the outcome in mail_log
    and updates the draft's status. Raises ValueError when the
    draft is not approved (send only after approval)."""
    draft = draft_mod.get_draft(conn, draft_id)
    if draft is None:
        raise ValueError(f"no draft {draft_id}")
    if draft["status"] != "approved":
        raise ValueError(
            f"draft {draft_id} is {draft['status']} — send only after approval"
        )
    transport = transport or mock_transport
    result = transport(draft["recipient"], draft["subject"], draft["body"])
    now = int(time.time())
    conn.execute(
        "INSERT INTO mail_log (id, draft_id, recipient, role_id,"
        " status, detail, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (f"log_{uuid.uuid4().hex[:12]}", draft_id, draft["recipient"],
         draft["role_id"], result.status, result.detail, now),
    )
    conn.execute(
        "UPDATE mail_drafts SET status = ?, updated_at = ? WHERE id = ?",
        (result.status, now, draft_id),
    )
    conn.commit()
    return result


def mail_log(conn, task_id: str | None = None) -> list[dict]:
    """The per-recruiter log: sent, bounced, no_reply."""
    if task_id:
        rows = conn.execute(
            "SELECT l.* FROM mail_log l JOIN mail_drafts d ON d.id = l.draft_id"
            " WHERE d.task_id = ? ORDER BY l.created_at ASC",
            (task_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM mail_log ORDER BY created_at ASC"
        ).fetchall()
    return [dict(r) for r in rows]
