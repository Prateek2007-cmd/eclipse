"""Cold mail (Sprint 5, plan Flow 2): drafts, approval, dedup,
send, and the per-recruiter log.

The flow: the user says "Cold-mail recruiters for the
shortlisted roles." Eclipse drafts one short personal mail per
recruiter (role, why this candidate fits, resume attached). The
user reads each draft, edits if needed, approves. Eclipse sends
only after approval. It logs sent, bounced, and no-reply per
recruiter. No duplicate mails to the same recruiter for the same
role.

The draft is a deterministic template (no model call) — the
planner can refine it, but the draft itself is built from the
role + the profile + the match reasons. The send transport is
pluggable: a mock transport for tests, the Gmail connector in
Sprint 7 (which is when real delivery + bounce detection land).

Tables (created by db.migrate()):
- mail_drafts: id, task_id, recipient, role_id, subject, body,
  status (draft/approved/sent/bounced/no_reply), created_at,
  updated_at.
- mail_log: id, draft_id, recipient, role_id, status, detail,
  created_at.
"""
