"""Chat API (PR #6): the Agent view is a real chat thread.

POST /chat/message {text} -> 200 {message_id, reply_text,
  kind: "chat" | "task_created", task_id, source: "planner" | "rules"}
GET  /chat/history?limit=50 -> {messages: [{id, role, text, kind,
  task_id, source, created_at}]} — oldest first, created_at unix
  SECONDS, source "planner"/"rules" on agent rows (null on user
  rows, so a reload still shows how each reply was made).

Token-authenticated like every other router. 422 on empty/blank
text (or over 2000 chars). No other endpoints: task progress is
rendered by the UI from the existing task endpoints.

How one message is answered (respond):
  1. Steering first, when the conversation has an active
     (non-terminal) task: stop/cancel/pause act on that task
     through the EXISTING task endpoints (tasks_api.cancel_task /
     pause_task — the same path the UI uses, claims released on
     cancel); a refinement appends to the task's stored
     constraints and never creates a task. A clear new-task
     instruction is never steering — it starts a new task.
  2. Small talk (greetings, thanks, goodbyes, capability
     questions) is answered by the rules at once — it never
     reaches the planner and can never create a task. This is the
     fix for the live-test bug: "hi" created two dead tasks.
  3. Everything else goes to the planner when one is configured
     (last 6 chat messages + recalled memory facts in the prompt,
     PII-redacted first — the loop.plan_task pattern). The planner
     classifies intent and drafts the reply. Any planner failure
     or unusable output falls back to the rules, labelled
     source "rules" — the same planner-or-labelled-rules pattern
     as the mission intake. Two guards: the planner may only
     upgrade a rules-ambiguous fragment to a task when the
     conversation gives it something to resolve against (with no
     history the rules win), and a planner failure never 500s a
     greeting (step 2 already answered those).
  4. A task is created through the SAME path as POST /tasks
     (loop.create_task — same validation, same gates), with the
     user's message as the goal. The created task is then driven
     by the existing loop + extension like any other task.

Storage: one chat_messages row per message, user and agent alike
(the thread IS the task conversation — no second memory store).
"""
from __future__ import annotations

import logging
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ..chat import classify
from ..core import db
from ..core.auth import require_token
from ..memory import keyword
from ..planner import router as planner_router
from ..planner import scan
from ..tasks import loop
from . import tasks_api

logger = logging.getLogger("eclipse.chat")

router = APIRouter(tags=["chat"])

MAX_MESSAGE_LEN = classify.MAX_MESSAGE_LEN
# Chat messages in the planner prompt (the brief's "last 6").
HISTORY_CONTEXT = 6


class MessageIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=MAX_MESSAGE_LEN)


def _conn():
    return db.connect()


def _store(conn, role: str, text: str, kind: str,
           task_id: str | None, source: str | None) -> str:
    """One chat_messages row. Returns its id."""
    message_id = f"msg-{uuid.uuid4().hex[:12]}"
    conn.execute(
        "INSERT INTO chat_messages (id, role, text, kind, task_id,"
        " source, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (message_id, role, text, kind, task_id, source, int(time.time())),
    )
    conn.commit()
    return message_id


def _answer(conn, text: str, reply_text: str, kind: str,
            task_id: str | None, source: str) -> dict:
    """Store the user message AND the agent reply, and build the
    POST /chat/message response. The message_id is the agent
    reply's row (the message the UI just received)."""
    _store(conn, "user", text, "chat", None, None)
    message_id = _store(conn, "agent", reply_text, kind, task_id, source)
    return {
        "message_id": message_id, "reply_text": reply_text,
        "kind": kind, "task_id": task_id, "source": source,
    }


def _recent_history(conn, limit: int = HISTORY_CONTEXT) -> list[dict]:
    """The last `limit` chat messages, oldest first, for the
    planner prompt. Ordered by (created_at, rowid): two messages
    stored in the same second still come out in insertion order."""
    rows = conn.execute(
        "SELECT role, text FROM chat_messages"
        " ORDER BY created_at DESC, rowid DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [{"role": r["role"], "text": r["text"]} for r in reversed(rows)]


def _memory_facts(conn, text: str) -> list[str]:
    # Mirror loop._memory_facts: recall() returns list[dict], the
    # prompt builders join strings (Bug 1: passing the dicts
    # through crashed plan_task with a 502).
    return [f["text"] for f in keyword.recall(conn, text, limit=10)]


def _active_task(conn) -> dict | None:
    """The most recent task linked to this conversation that is
    still active (not cancelled / failed / done). The terminal
    set comes from tasks_api, so the two cannot drift apart."""
    placeholders = ", ".join("?" * len(tasks_api.TERMINAL_TASK_STATUSES))
    row = conn.execute(
        "SELECT t.* FROM chat_messages m JOIN tasks t ON t.id = m.task_id"
        f" WHERE t.status NOT IN ({placeholders})"
        " ORDER BY m.created_at DESC, m.rowid DESC LIMIT 1",
        tasks_api.TERMINAL_TASK_STATUSES,
    ).fetchone()
    return dict(row) if row else None


def _append_refinement(conn, task_id: str, text: str) -> None:
    """Append a mid-task refinement to the task's stored
    constraints. The loop re-reads the task row on every step
    (execute_step puts CONSTRAINTS in each step's prompt), so a
    refinement shapes every step still to run; a task still in
    "created" gets it in its plan too. The plan's existing step
    list does not change — the reply says so honestly."""
    task = loop.get_task(conn, task_id)
    old = (task.get("constraints") or "") if task else ""
    new = (old + "\n" if old else "") + f"Refinement: {text}"
    conn.execute(
        "UPDATE tasks SET constraints = ?, updated_at = ? WHERE id = ?",
        (new[-2000:], int(time.time()), task_id),
    )
    conn.commit()


def _capitalised(label: str) -> str:
    return label[0].upper() + label[1:] if label else label


def respond(conn, text: str, call_fn=None) -> dict:
    """Answer one chat message (see the module docstring for the
    decision order). `call_fn` is the injectable planner call —
    router.call_planner by default, a fake in tests. Raises
    ValueError only for an empty or oversized message (the
    endpoint maps it to 422); a planner failure never raises."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("text must not be empty")
    if len(text) > MAX_MESSAGE_LEN:
        raise ValueError(
            f"text must be at most {MAX_MESSAGE_LEN} characters"
        )
    text = text.strip()
    history = _recent_history(conn)
    active = _active_task(conn)

    # 1. Steering: an active task is steered before anything else.
    steering = classify.classify_steering(text)
    if active is not None and steering in ("cancel", "pause"):
        label = classify.task_label(active["goal"])
        try:
            if steering == "cancel":
                # The EXISTING cancel path (same as POST
                # /tasks/{id}/cancel): terminal-state guard +
                # role claims released.
                tasks_api.cancel_task(active["id"])
                reply = f"Stopped {label}."
            else:
                tasks_api.pause_task(active["id"])
                reply = (
                    f"Paused {label} — resume it from the task"
                    " view whenever you're ready."
                )
        except HTTPException:
            # Raced with the task finishing: say so honestly.
            verb = "stop" if steering == "cancel" else "pause"
            reply = (
                f"{_capitalised(label)} already finished, so there"
                f" was nothing to {verb}."
            )
        return _answer(conn, text, reply, "chat", active["id"], "rules")
    if active is not None and steering == "refine":
        phrase = classify.refinement_phrase(text)
        _append_refinement(conn, active["id"], text)
        if active["status"] == "created":
            reply = (
                f"Noted — {phrase}. I've added it to the task,"
                " so the plan will follow it."
            )
        else:
            reply = (
                f"Noted — {phrase}. I've added it to the task's"
                " rules, so the steps still to run follow it."
            )
        return _answer(conn, text, reply, "chat", active["id"], "rules")
    if active is None and steering in ("cancel", "pause"):
        verb = "stop" if steering == "cancel" else "pause"
        return _answer(
            conn, text,
            f"There's no task running right now — nothing to {verb}.",
            "chat", None, "rules",
        )

    # 2. Small talk: rules answer at once, never the planner.
    decision = classify.classify_rules(text)
    if decision["hard_chat"]:
        return _answer(
            conn, text, decision["reply_text"], "chat", None, "rules"
        )

    # 3. Planner first (when one is configured), rules on any
    # failure — the mission-intake pattern, labelled.
    call_fn = call_fn or planner_router.call_planner
    try:
        facts = _memory_facts(conn, text)
        prompt = classify.build_chat_prompt(text, history, facts)
        # Second PII scan before the outbound call (§6.4), the
        # loop.plan_task pattern.
        prompt, _hits = scan.redact(prompt)
        raw, _model, _ms = call_fn(prompt)
        parsed = classify.parse_planner_reply(raw)
        if (
            parsed["kind"] == "task_created"
            and decision["reason"] == "ambiguous"
            and not history
        ):
            # The planner upgraded a context-free fragment to a
            # task — "internships" with no conversation behind it
            # is not an instruction. The rules win; with history
            # (a reply to a clarifying question) the planner's
            # resolution stands.
            raise ValueError(
                "planner upgraded a context-free fragment to a task"
            )
        kind = parsed["kind"]
        reply_text = parsed["reply_text"]
        constraints = parsed.get("constraints") or ""
        source = "planner"
    except Exception as exc:  # noqa: BLE001 - any planner failure falls back to rules
        logger.info("chat planner path failed (%s); using rules", exc)
        kind = decision["kind"]
        reply_text = decision["reply_text"]
        constraints = ""
        source = "rules"

    # 4. Task creation goes through the SAME path as POST /tasks.
    task_id = None
    if kind == "task_created":
        task_id = loop.create_task(conn, text, constraints)
    return _answer(conn, text, reply_text, kind, task_id, source)


@router.post("/chat/message")
def post_message(body: MessageIn, _token: str = Depends(require_token)):
    if not body.text.strip():
        raise HTTPException(status_code=422, detail="text must not be blank")
    conn = _conn()
    try:
        try:
            return respond(conn, body.text)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        conn.close()


@router.get("/chat/history")
def get_history(limit: int = Query(default=50, ge=1, le=200),
                _token: str = Depends(require_token)):
    """The thread, oldest first. `limit` keeps the most recent N
    messages (a long thread returns its tail, not its head)."""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT id, role, text, kind, task_id, source, created_at"
            " FROM chat_messages ORDER BY created_at DESC, rowid DESC"
            " LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    messages = [dict(r) for r in reversed(rows)]
    return {"messages": messages}
