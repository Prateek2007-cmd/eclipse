"""Cold-mail send + log (Sprint 5, plan Flow 2; real delivery in
Sprint 7).

Send only after approval: a draft must be 'approved' and the
task must hold a valid approval grant (the same grant machinery
as Sprint 4 — approval BEFORE the send, §5.4). The send is
recorded in mail_log with the outcome per recruiter: sent,
bounced, or no_reply.

The transport is pluggable. The mock transport (used by tests)
decides by the recipient address: an address containing
"bounce" bounces, one containing "noreply" never replies, and
anything else is sent. The default transport is the Gmail
connector (Sprint 7): real delivery through the user's connected
Gmail, with the resume attached when the caller asks. A bounce
is detected afterwards by check_bounces_for_task — a delivery
failure notice arrives minutes after the send, so the send
records "sent" and a later check updates it to "bounced".
"""
from __future__ import annotations

import os
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


def _default_transport(task_id: str, attach: str | None):
    """The real Gmail transport — unless the test env var says
    mock. ECLIPSE_MAIL_TRANSPORT=mock is TEST-ONLY: it makes
    send_draft use the mock transport instead of the user's real
    Gmail. Never set it outside tests. When it is set, a loud
    warning is logged on every use, so a leaked test env can
    never silently fake a send in production."""
    if os.environ.get("ECLIPSE_MAIL_TRANSPORT") == "mock":
        import logging

        logging.getLogger("eclipse.mail").warning(
            "ECLIPSE_MAIL_TRANSPORT=mock — mail is NOT being sent "
            "(test mode). Never set this outside tests."
        )
        return mock_transport
    return gmail_transport(task_id, attach=attach)


def _require_gmail_on(conn) -> None:
    """Fail closed when the Gmail connector is off or
    unregistered. The connectors API enforces the same rule for
    its own Gmail endpoints (_require_on) — but the send path
    built GmailConnector directly, so disabling Gmail in the
    workspace stopped nothing: Eclipse still sent mail and
    still read the inbox for bounces. A missing row is not
    "on"."""
    from server.connectors import registry as conn_registry

    reg = conn_registry.get(conn, "gmail")
    if reg is None or not reg["enabled"]:
        raise ValueError("gmail connector is off — enable it first")


def gmail_transport(task_id: str, attach: str | None = None):
    """Build the real Gmail transport for one task.

    Returns a (recipient, subject, body) -> SendResult callable
    that sends through the Gmail connector, attaching the resume
    when `attach` names a file. Never retried: a ConnectorError
    propagates to the caller.
    """
    from server.connectors.gmail import GmailConnector

    connector = GmailConnector()

    def _send(recipient: str, subject: str, body: str) -> SendResult:
        result = connector.send(task_id, recipient, subject, body, attach=attach)
        detail = result.get("message_id") or "sent via Gmail"
        return SendResult("sent", detail)

    return _send


def send_draft(conn, draft_id: str, transport=None, task_id: str | None = None,
               attach: str | None = None) -> SendResult:
    """Send an approved draft. Records the outcome in mail_log
    and updates the draft's status. Raises ValueError when the
    draft is not approved (send only after approval).

    `transport` is (recipient, subject, body) -> SendResult. When
    omitted, the real Gmail transport is used (Sprint 7); pass
    mock_transport in tests.
    """
    draft = draft_mod.get_draft(conn, draft_id)
    if draft is None:
        raise ValueError(f"no draft {draft_id}")
    if draft["status"] != "approved":
        raise ValueError(
            f"draft {draft_id} is {draft['status']} — send only after approval"
        )
    # A bounced address never receives another mail (§7.1). The
    # bounce is recorded when it is found — which may be AFTER
    # this draft was approved — so the block is checked here, at
    # send time, not only when the draft is created. The draft
    # is marked bounced: it can never be sent to this address,
    # and leaving it 'approved' would strand it as approved but
    # un-sendable. (In the API flow this commit also commits the
    # grant consumption — the grant named this draft only, so it
    # is useless once the draft is bounced.)
    if draft_mod.has_bounced(conn, draft["recipient"]):
        now = int(time.time())
        conn.execute(
            "UPDATE mail_drafts SET status = 'bounced', updated_at = ? WHERE id = ?",
            (now, draft_id),
        )
        conn.commit()
        raise ValueError(
            f"recipient {draft['recipient']} bounced — send refused"
        )
    task = task_id or draft["task_id"]
    if transport is None:
        # Only the real path is gated: a caller that supplies its
        # own transport (tests) is responsible for it.
        _require_gmail_on(conn)
    transport = transport or _default_transport(task, attach)
    try:
        result = transport(draft["recipient"], draft["subject"], draft["body"])
    except Exception as exc:  # noqa: BLE001 - every failure is logged
        # A failed attempt is still an attempt: log it (status
        # 'failed') so the send log is complete, then re-raise.
        # The draft stays approved and can be re-approved for a
        # retry (the API's grant is consumed by this commit —
        # the approval authorised one send attempt, and the
        # attempt happened). The detail is redacted before it is
        # stored — a connector error can carry addresses.
        from ..planner import scan

        clean_detail, _hits = scan.redact(str(exc)[:500])
        now = int(time.time())
        conn.execute(
            "INSERT INTO mail_log (id, draft_id, recipient, role_id,"
            " status, detail, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (f"log_{uuid.uuid4().hex[:12]}", draft_id, draft["recipient"],
             draft["role_id"], "failed", clean_detail, now),
        )
        conn.commit()
        raise
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


def check_bounces_for_task(conn, task_id: str, gmail=None) -> list[dict]:
    """Check the inbox for delivery failures for this task's sent
    mails. A draft logged as "sent" whose recipient bounced is
    updated to "bounced" (and the draft row too). Plan §7.1: a
    bounce stops further sends to that address — already_mailed
    blocks the address from then on. Each recipient is checked
    and committed on its own, so one provider failure does not
    discard the updates already made.

    Returns [{recipient, bounced, detail}] for every sent mail
    checked.
    """
    from server.connectors.gmail import GmailConnector

    if gmail is None:
        # Same gate as send_draft: a disabled Gmail must not be
        # read for bounces either. An injected connector is the
        # caller's responsibility and is not gated.
        _require_gmail_on(conn)
    connector = gmail or GmailConnector()
    rows = conn.execute(
        "SELECT l.id AS log_id, l.recipient, l.draft_id FROM mail_log l"
        " JOIN mail_drafts d ON d.id = l.draft_id"
        " WHERE d.task_id = ? AND l.status = 'sent'",
        (task_id,),
    ).fetchall()
    out = []
    for row in rows:
        try:
            found = connector.check_bounce(row["recipient"])
        except Exception as exc:  # noqa: BLE001 - one bad check
            out.append(
                {
                    "recipient": row["recipient"],
                    "bounced": False,
                    "detail": f"bounce check failed: {exc}",
                }
            )
            continue
        out.append(
            {
                "recipient": row["recipient"],
                "bounced": found["bounced"],
                "detail": found.get("detail", ""),
            }
        )
        if found["bounced"]:
            now = int(time.time())
            conn.execute(
                "UPDATE mail_log SET status = 'bounced', detail = ? WHERE id = ?",
                (found.get("detail", ""), row["log_id"]),
            )
            conn.execute(
                "UPDATE mail_drafts SET status = 'bounced', updated_at = ? WHERE id = ?",
                (now, row["draft_id"]),
            )
            conn.commit()
    return out


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
