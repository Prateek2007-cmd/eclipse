"""Missions (PR #5): standing chat-driven missions — job hunt, cold
mail, price watch — built on the existing task loop, matcher,
drafts, watches and briefing pipes. One mission = one task per run;
all state lives in SQLite, all caps are counted from the activity
log, and every consequential action goes through the existing
pending-approval gate (or the Q1 standing-approval auto-satisfy
path, which uses the same gate — see server/tasks/approvals.py).
"""
