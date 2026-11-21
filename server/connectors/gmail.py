"""Gmail connector (Sprint 7, plan §7.1).

Scopes (minimal, held by the platform connector): gmail.readonly,
gmail.send. No delete, no settings scope.

- Read: search the inbox for offer letters and interview mails
  (keywords: offer, interview, shortlisted, selected, application
  status). Bodies AND subjects are redacted before any planner
  use — a subject line can itself carry a name, phone or email.
- Send: only after the user approved the exact draft (§5.4), unless
  a scoped allowance exists for that recipient. Logs sent /
  bounced / no-reply per recipient. Dedup: never send twice to the
  same recruiter for the same role (checked in mail/drafts.py).
- Rate limits: max 10 sends per task, 1 send per 5 seconds. A
  bounce stops further sends to that address and tells the user
  (plan §7.1 — a bounced address is not retried, even with a new
  approval; a corrected address is a different recipient).

Bounce detection: a bounce arrives minutes after the send, as a
Delivery Status Notification from mailer-daemon. `check_bounce`
searches the inbox for one naming the recipient, scoped to the
last 7 days; the mail log is updated by
`check_bounces_for_task` in server/mail/send.py.
"""
from __future__ import annotations

import re
import threading
import time

from server.planner import scan

from .base import ConnectorError, GwsCli

OFFER_KEYWORDS = ("offer", "interview", "shortlisted", "selected", "application status")
MAX_SENDS_PER_TASK = 10
# Seconds between sends for one task. Tests set this to 0 —
# 10 real sends would otherwise take ~45s of sleeps.
SEND_INTERVAL_S = 5

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")

# Per-task send timestamps, for the 1-per-5s rate limit. In
# memory only, guarded by _send_lock: a server restart resets
# the counts, so the 10/task limit is enforced within a server
# run, not across restarts (accepted — the limit exists to avoid
# spamming the provider, and a restart is a deliberate operator
# action, not a send path).
_send_lock = threading.Lock()
_last_send_at: dict[str, float] = {}
_send_counts: dict[str, int] = {}


def _offer_query() -> str:
    keywords = " OR ".join(f'"{k}"' for k in OFFER_KEYWORDS)
    return f"in:inbox ({keywords})"


class GmailConnector:
    """Gmail via hatch_gws_cli. `cli` is injectable for tests."""

    def __init__(self, cli: GwsCli | None = None):
        self.cli = cli or GwsCli()

    def status(self) -> dict:
        """connected / needs_reconnect / unavailable + connect_url."""
        try:
            data = self.cli.status("gmail")
        except ConnectorError as exc:
            if exc.kind == "auth":
                return {"name": "gmail", "state": "needs_reconnect", "error": str(exc)}
            return {"name": "gmail", "state": "unavailable", "error": str(exc)}
        if not isinstance(data, dict):
            return {"name": "gmail", "state": "unavailable", "error": "unexpected status output"}
        if data.get("status") == "connected":
            return {"name": "gmail", "state": "connected"}
        return {
            "name": "gmail",
            "state": "needs_reconnect",
            "connect_url": data.get("connect_url"),
        }

    def search_offers(self, max_results: int = 20) -> list[dict]:
        """Offer/interview mails from the inbox, bodies and
        subjects redacted.

        Returns [{id, from, subject, date, body_redacted}]. A body
        that still matches a PII pattern after redaction is dropped
        rather than returned.
        """
        data = self.cli.run_read(
            "gmail", "+triage",
            "--query", _offer_query(),
            "--max", str(max_results),
            "--format", "json",
        )
        messages = data.get("messages", []) if isinstance(data, dict) else []
        out = []
        for msg in messages:
            body = (msg.get("body") or msg.get("snippet") or "")
            clean, _ = scan.redact(body)
            if scan.scan(clean):
                continue
            subject, _ = scan.redact(msg.get("subject") or "")
            out.append(
                {
                    "id": msg.get("id"),
                    "from": msg.get("from"),
                    "subject": subject,
                    "date": msg.get("date"),
                    "body_redacted": clean,
                }
            )
        return out

    def send(
        self,
        task_id: str,
        to: str,
        subject: str,
        body: str,
        attach: str | None = None,
    ) -> dict:
        """Send one mail via Gmail. Rate-limited, never retried.

        Returns {sent: True, message_id} or raises ConnectorError.
        The caller (mail/send.py) owns the approval grant, the dedup
        check and the log row — this only performs the send.
        """
        with _send_lock:
            count = _send_counts.get(task_id, 0)
            if count >= MAX_SENDS_PER_TASK:
                raise ConnectorError(
                    "gmail",
                    f"send limit reached for this task ({MAX_SENDS_PER_TASK})",
                    "quota",
                )
            last = _last_send_at.get(task_id, 0.0)
            wait = SEND_INTERVAL_S - (time.time() - last)
            if wait > 0:
                time.sleep(wait)
            argv = [
                "gmail", "+send",
                "--to", to,
                "--subject", subject,
                "--body", body,
            ]
            if attach:
                argv += ["--attach", attach]
            data = self.cli.run(*argv)
            _last_send_at[task_id] = time.time()
            _send_counts[task_id] = count + 1
        message_id = None
        if isinstance(data, dict):
            message_id = data.get("message_id") or data.get("id")
        return {"sent": True, "message_id": message_id}

    def check_bounce(self, recipient: str) -> dict:
        """Look for a Delivery Status Notification naming this
        recipient in the last 7 days. Returns {bounced, detail}."""
        if not _EMAIL_RE.match(recipient or ""):
            raise ConnectorError("gmail", f"not an email address: {recipient}", "error")
        query = (
            'in:inbox newer_than:7d (from:mailer-daemon OR subject:"Delivery Status'
            f' Notification" OR subject:"Undeliverable") "{recipient}"'
        )
        data = self.cli.run_read(
            "gmail", "+triage",
            "--query", query,
            "--max", "5",
            "--format", "json",
        )
        messages = data.get("messages", []) if isinstance(data, dict) else []
        if not messages:
            return {"bounced": False, "detail": ""}
        first = messages[0]
        return {
            "bounced": True,
            "detail": first.get("subject", "delivery failure notice"),
        }
