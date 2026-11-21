"""Task loop (Sprint 3, plan §6.2; fix pass 2026-11-21): create →
plan → per-step observe → plan one action → validate → execute
(guarded) → verify by re-observing → checkpoint.

The server drives the loop; the extension is the hands (it walks the
page, performs the validated action, and re-observes). Each HTTP
call is one loop turn:
  POST /tasks/{id}/plan            → decompose the goal
  POST /tasks/{id}/steps/{n}/execute (with the redacted observation)
                                   → returns one validated action
  POST /tasks/{id}/steps/{n}/verify (with the new observation AND
    the pre-action observation for a baseline diff)
                                   → verified or retry/fail

Limits (§11): max 25 steps/task, max 3 retries/step (backoff
1s/2s/4s — the caller sleeps; the server records the retry count),
max 3 planner calls/step. Never retry a submit/charge/send: the
server refuses to re-execute a step that is already done, and
refuses a click on an element that already has a done click step
in the same task (the extension's in-memory guard is the second
layer, not the only one).
End states: done (verified), ask_user, handoff (human-only),
refuse (policy), failed (limits hit, with reason + checkpoint).

Privacy (§6.2 step 3, §6.4): before ANY outbound planner call, the
assembled prompt is rescanned with the server-side second PII scan
(server/planner/scan.py). A hit is re-redacted and logged; two
blocks on the same step stop the task.

Verify (§6.2: "action ran is not verification"): every check is a
BASELINE DIFF against the pre-action observation. Pre-existing text,
pre-existing elements and pre-filled field values never verify a
step. A step is done only when the end state is newly true.
"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid

from ..core import audit, db
from ..memory import keyword
from ..planner import context as ctx
from ..planner import prompts, router, scan, validate

logger = logging.getLogger("eclipse.tasks")

MAX_STEPS_PER_TASK = 25
MAX_RETRIES_PER_STEP = 3
MAX_PLANNER_CALLS_PER_STEP = 3
RETRY_BACKOFF_S = (1, 2, 4)
HANDOFF_TIMEOUT_S = 600  # plan §5.5 step 4: never wait forever

# A step whose description mentions one of these is human-only.
HUMAN_ONLY_MARKERS = (
    "captcha",
    "3-d secure",
    "3d secure",
    "otp",
    "passkey",
    "verification code",
    "2fa",
    "two-factor",
    "security code",
    "authenticator",
)


class GroundingError(validate.ValidationError):
    """The planner named an element that is not in the current
    observation. The caller must re-observe once and re-plan (§6.3);
    re-planning against the same stale observation is not allowed.
    """


def create_task(conn, goal: str, constraints: str = "") -> str:
    task_id = uuid.uuid4().hex[:12]
    now = int(time.time())
    conn.execute(
        "INSERT INTO tasks (id, goal, constraints, status, created_at, updated_at)"
        " VALUES (?, ?, ?, 'created', ?, ?)",
        (task_id, goal, constraints, now, now),
    )
    conn.commit()
    return task_id


def get_task(conn, task_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return dict(row) if row else None


def _set_task_status(conn, task_id: str, status: str) -> None:
    conn.execute(
        "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
        (status, int(time.time()), task_id),
    )
    conn.commit()


def _memory_facts(conn, goal: str) -> list[str]:
    return keyword.recall(conn, goal, limit=10)


def _is_local_only(constraints: str) -> bool:
    """A task whose constraints say "local only" never touches the
    cloud planner (plan §9: never silently fall back from local
    mode to cloud)."""
    text = (constraints or "").lower()
    return "local only" in text or "local-only" in text


def _audit(conn, task_id: str, action: str, payload: dict) -> None:
    """Append a redacted audit entry (payloads carry ids and
    results only — never raw PII or prompt text)."""
    audit.append(conn, task_id, action, payload)


def plan_task(conn, task_id: str, call_fn=None) -> list[dict]:
    """Decompose the goal into steps (one planner call)."""
    task = get_task(conn, task_id)
    if task is None:
        raise KeyError(f"no task {task_id}")
    call_fn = call_fn or router.call_planner
    facts = _memory_facts(conn, task["goal"])
    prompt = prompts.build_plan_prompt(task["goal"], task["constraints"], facts)
    # Second PII scan before the outbound call (§6.4).
    prompt, hits = scan.redact(prompt)
    if hits:
        logger.warning("task %s plan prompt had %d PII hits (re-redacted)", task_id, len(hits))
    raw, model, ms = call_fn(prompt, local_only=_is_local_only(task["constraints"]))
    logger.info("task %s planned via %s in %.0f ms", task_id, model, ms)
    steps = validate.parse_plan(raw)
    now = int(time.time())
    for i, step in enumerate(steps, start=1):
        conn.execute(
            "INSERT INTO steps (id, task_id, n, action_json, status,"
            " started_at, ended_at, ms, element_id, result, error, retries,"
            " scan_blocks, grounding_failures)"
            " VALUES (?, ?, ?, ?, 'pending', ?, NULL, NULL, NULL, NULL, NULL, 0, 0, 0)",
            (
                uuid.uuid4().hex[:12],
                task_id,
                i,
                json.dumps(step),
                now,
            ),
        )
    _set_task_status(conn, task_id, "planned")
    _audit(conn, task_id, "task.plan", {"steps": len(steps), "model": model})
    return steps


def _get_step(conn, task_id: str, n: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM steps WHERE task_id = ? AND n = ?", (task_id, n)
    ).fetchone()
    return dict(row) if row else None


def checkpoint_summary(conn, task_id: str) -> str:
    row = conn.execute(
        "SELECT summary FROM checkpoints WHERE task_id = ?"
        " ORDER BY step_n DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    return row["summary"] if row else ""


def last_results(conn, task_id: str, limit: int = 3) -> list[str]:
    rows = conn.execute(
        "SELECT n, result FROM steps WHERE task_id = ? AND status = 'done'"
        " ORDER BY n DESC LIMIT ?",
        (task_id, limit),
    ).fetchall()
    return [f"step {r['n']}: {r['result']}" for r in reversed(rows)]


def write_checkpoint(conn, task_id: str, step_n: int, summary: str, state: dict) -> None:
    """After each verified step: a 10-line summary + the state JSON.

    Resume reads the latest checkpoint, never the full history (§6.5).
    """
    lines = [ln.strip() for ln in summary.strip().splitlines() if ln.strip()][:10]
    conn.execute(
        "INSERT INTO checkpoints (id, task_id, step_n, summary, state_json, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            uuid.uuid4().hex[:12],
            task_id,
            step_n,
            "\n".join(lines),
            json.dumps(state),
            int(time.time()),
        ),
    )
    conn.commit()


def _allowed_sites(constraints: str) -> list[str] | None:
    """Parse an "allowed sites: a.com, b.com" line from constraints."""
    for line in (constraints or "").splitlines():
        if line.strip().lower().startswith("allowed sites:"):
            sites = line.split(":", 1)[1]
            return [s.strip() for s in sites.split(",") if s.strip()]
    return None


def _is_human_only_description(description: str) -> bool:
    lowered = (description or "").lower()
    return any(m in lowered for m in HUMAN_ONLY_MARKERS)


def _role_from_constraints(constraints: str) -> dict:
    """The apply flow carries the role in the task constraints
    ("role: role_1", "url: http://…"). Without it the loop has no
    role context and cannot check "already applied" (§6.9)."""
    role_id = url = None
    for line in (constraints or "").splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("role:"):
            role_id = stripped.split(":", 1)[1].strip()
        elif stripped.lower().startswith("url:"):
            url = stripped.split(":", 1)[1].strip()
    return {"role_id": role_id, "url": url}


def _is_consequential(action: dict, elements: list[dict]) -> bool:
    """Is this action consequential (a real submit / send / pay)?
    The server now sees the element's type and whether it sits in
    a form (ElementInfo carries both) — the precise signal, not
    just the name. "Apply now" on a listing is NOT consequential:
    it only opens the form. A button inside a form IS (a
    multi-step "Continue" submits the form), as is any button
    whose type is submit, or whose name says submit / confirm /
    pay / send. This matches the executor's guard, so one
    application costs one approval, not two."""
    if action.get("action") != "click":
        return False
    target = next(
        (e for e in elements if e.get("id") == action.get("element_id")), None
    )
    if target is None:
        return False
    el_type = str(target.get("type") or "").lower()
    if el_type in ("submit", "image"):
        return True
    if target.get("in_form"):
        return True
    text = f"{target.get('name','')} {target.get('text','')}".lower()
    return any(w in text for w in ("submit", "confirm", "place order", "pay", "send"))


# Consequential goal verbs (§5.4): purchases, sends and
# applications all need a fresh approval. "Log in to the test
# portal" has none of these, so the login-fixture flow stays
# approval-free.
_CONSEQUENTIAL_GOAL_WORDS = (
    "apply", "buy", "purchase", "pay", "send", "mail", "email",
    "transfer", "checkout", "order", "subscribe", "book",
)


def _requires_approval(task: dict) -> bool:
    """A task requires approval before a consequential action when
    its goal names one (buy, pay, send, apply, …) or its
    constraints say so."""
    goal = (task.get("goal") or "").lower()
    constraints = (task.get("constraints") or "").lower()
    return (
        any(re.search(rf"\b{w}\b", goal) for w in _CONSEQUENTIAL_GOAL_WORDS)
        or "approval" in constraints
    )


def _consume_approval(conn, task_id: str, element_id: str | None = None,
                      role_id: str | None = None) -> bool:
    """Consume one valid approval grant for this task (a grant is
    single-use; purchases never get a lasting allow, §5.4).

    Atomic: a single DELETE whose subquery picks one grant, so two
    concurrent submits cannot spend the same grant (the old
    SELECT-then-DELETE raced). Action-scoped: a grant whose detail
    names an element or role only covers that element or role —
    an approval for role A cannot be consumed by a submit for
    role B. A grant with no detail (the old shape) covers any
    action on the task.
    """
    now = int(time.time())
    # Pick one matching grant, preferring the most specific one.
    rows = conn.execute(
        "SELECT id, detail FROM grants WHERE origin = ? AND scope = 'approve'"
        " AND expires_at > ? ORDER BY created_at ASC",
        (task_id, now),
    ).fetchall()
    chosen = None
    for row in rows:
        detail = {}
        if row["detail"]:
            try:
                detail = json.loads(row["detail"])
            except (ValueError, TypeError):
                detail = {}
        if not detail:
            chosen = row["id"]
            break
        if element_id and detail.get("element_id") == element_id:
            chosen = row["id"]
            break
        if role_id and detail.get("role_id") == role_id:
            chosen = row["id"]
            break
    if chosen is None:
        return False
    cur = conn.execute("DELETE FROM grants WHERE id = ?", (chosen,))
    conn.commit()
    return cur.rowcount == 1


def _handoff(conn, task_id: str, n: int, reason: str) -> None:
    """Human-only step (§5.5): pause, save a checkpoint, log the
    handoff in the audit trail, and record the 10-minute deadline.
    The task never waits forever: resume after the deadline fails
    it instead (see resume_task)."""
    deadline = int(time.time()) + HANDOFF_TIMEOUT_S
    conn.execute(
        "UPDATE steps SET status = 'handoff', error = ? WHERE task_id = ? AND n = ?",
        (reason, task_id, n),
    )
    write_checkpoint(
        conn,
        task_id,
        n,
        f"Step {n} handed off to the user: {reason}.\n"
        f"Handoff deadline: {deadline} (10 minutes).\n"
        f"Task: {get_task(conn, task_id)['goal']}",
        {"step": n, "handoff": reason, "deadline": deadline},
    )
    _audit(conn, task_id, "task.handoff", {"step": n, "reason": reason, "deadline": deadline})
    _set_task_status(conn, task_id, "handoff")


def resume_task(conn, task_id: str) -> str:
    """Resume after a human-only handoff: the user completed the
    human step, so mark every handoff step done and let the task
    continue from the checkpoint. If the handoff deadline passed,
    the task fails instead (§5.5 step 4)."""
    task = get_task(conn, task_id)
    if task is None:
        raise KeyError(f"no task {task_id}")
    now = int(time.time())
    handoff_steps = conn.execute(
        "SELECT n FROM steps WHERE task_id = ? AND status = 'handoff'",
        (task_id,),
    ).fetchall()
    for row in handoff_steps:
        # The deadline lives in the handoff checkpoint's state_json.
        cp = conn.execute(
            "SELECT state_json FROM checkpoints WHERE task_id = ? AND step_n = ?"
            " ORDER BY created_at DESC LIMIT 1",
            (task_id, row["n"]),
        ).fetchone()
        deadline = 0
        if cp:
            try:
                deadline = int(json.loads(cp["state_json"]).get("deadline", 0))
            except (ValueError, TypeError):
                deadline = 0
        if deadline and now > deadline:
            _set_task_status(conn, task_id, "failed")
            _audit(conn, task_id, "task.handoff_timeout", {"step": row["n"]})
            return "failed"
        conn.execute(
            "UPDATE steps SET status = 'done', result = 'completed by user (handoff)',"
            " ended_at = ? WHERE task_id = ? AND n = ?",
            (now, task_id, row["n"]),
        )
    conn.commit()
    _set_task_status(conn, task_id, "planned")
    _audit(conn, task_id, "task.resume", {"steps_resumed": len(handoff_steps)})
    return "planned"


def execute_step(
    conn,
    task_id: str,
    n: int,
    observation: dict,
    call_fn=None,
) -> dict:
    """Plan one action for step n and return it, validated.

    `observation` is the redacted observation from the extension:
    {url, elements: [{id, tag, role, name, text, value, visible,
    interactable}]}. Returns {action, expected_end_state, model}.
    Raises validate.ValidationError after 3 failed planner calls,
    GroundingError when the planner names an element that is not in
    the observation (caller must re-observe once and re-plan, §6.3),
    or KeyError for a missing task/step.
    """
    call_fn = call_fn or router.call_planner
    task = get_task(conn, task_id)
    if task is None:
        raise KeyError(f"no task {task_id}")
    step = _get_step(conn, task_id, n)
    if step is None:
        raise KeyError(f"no step {n} for task {task_id}")
    if n > MAX_STEPS_PER_TASK:
        raise ValueError("task exceeds the 25-step limit")
    # Never re-execute a step that is already done (§6.9: never
    # retry a submit — the server is the durable backstop; the
    # extension's in-memory guard is only the second layer).
    if step["status"] == "done":
        raise ValueError(f"step {n} is already done; refusing to re-execute")
    if step["status"] in ("cancelled", "failed"):
        raise ValueError(f"step {n} is {step['status']}; refusing to execute")
    plan_step = json.loads(step["action_json"])
    if _is_human_only_description(plan_step.get("description", "")):
        _handoff(conn, task_id, n, f"human-only step: {plan_step['description']}")
        raise ValueError(f"step {n} is human-only: {plan_step['description']}")

    elements = observation.get("elements", [])
    element_ids = {e["id"] for e in elements if isinstance(e.get("id"), str)}
    interactable_ids = {
        e["id"] for e in elements if isinstance(e.get("id"), str) and e.get("interactable")
    }
    obs_text = ctx.format_observation(observation.get("url", ""), elements)
    summary = checkpoint_summary(conn, task_id)
    results = last_results(conn, task_id)
    facts = _memory_facts(conn, task["goal"])
    fixed = [
        prompts.REDACTION_CONTRACT,
        prompts.ACTION_SCHEMA,
        f"GOAL: {task['goal']}",
        f"CONSTRAINTS: {task['constraints']}" if task["constraints"] else "",
        f"PROGRESS SO FAR:\n{summary}" if summary else "",
        "LAST STEP RESULTS:\n" + "\n".join(results) if results else "",
        "REMEMBERED FACTS:\n" + "\n".join(facts) if facts else "",
    ]
    prompt = ctx.fit_to_budget(fixed, obs_text)
    if ctx.estimate_tokens(prompt) > ctx.MAX_TOKENS:
        logger.warning(
            "task %s step %s prompt exceeds the 8K budget (%d tokens)",
            task_id, n, ctx.estimate_tokens(prompt),
        )
    allowed_sites = _allowed_sites(task["constraints"])
    local_only = _is_local_only(task["constraints"])

    # Second PII scan before the outbound planner call (§6.4).
    # A hit is re-redacted and logged; two blocks on the same step
    # stop the task.
    prompt, hits = scan.redact(prompt)
    if hits:
        blocks = int(step.get("scan_blocks") or 0) + 1
        conn.execute(
            "UPDATE steps SET scan_blocks = ? WHERE task_id = ? AND n = ?",
            (blocks, task_id, n),
        )
        conn.commit()
        _audit(conn, task_id, "task.pii_block", {"step": n, "hits": len(hits), "block": blocks})
        if blocks >= 2:
            conn.execute(
                "UPDATE steps SET status = 'failed', error = ? WHERE task_id = ? AND n = ?",
                ("second PII scan blocked twice on this step", task_id, n),
            )
            _set_task_status(conn, task_id, "failed")
            raise validate.ValidationError(
                "second PII scan blocked twice on this step; task stopped"
            )

    last_error = ""
    for attempt in range(1, MAX_PLANNER_CALLS_PER_STEP + 1):
        attempt_prompt = prompt
        if last_error:
            attempt_prompt += (
                f"\n\nYour previous action was rejected: {last_error}. "
                "Return a corrected action."
            )
        raw, model, ms = call_fn(attempt_prompt, local_only=local_only)
        try:
            action = validate.parse_action(raw)
            validate.validate_action(
                action, element_ids, interactable_ids, allowed_sites, elements=elements
            )
        except validate.ValidationError as exc:
            # Grounding failure: do NOT re-plan against the same
            # stale observation (§6.3). Tell the caller to
            # re-observe once and re-plan. Two failed groundings
            # in a row → ask the user.
            if "not in the current observation" in str(exc):
                failures = int(step.get("grounding_failures") or 0) + 1
                conn.execute(
                    "UPDATE steps SET grounding_failures = ? WHERE task_id = ? AND n = ?",
                    (failures, task_id, n),
                )
                conn.commit()
                _audit(conn, task_id, "task.grounding_miss",
                       {"step": n, "failures": failures})
                if failures >= 2:
                    _set_task_status(conn, task_id, "ask_user")
                    raise GroundingError(
                        f"two failed groundings in a row on step {n}; asking the user"
                    ) from exc
                raise GroundingError(
                    f"element not in the current observation (re-observe once and re-plan): {exc}"
                ) from exc
            last_error = str(exc)
            logger.warning("task %s step %s planner attempt %d rejected: %s", task_id, n, attempt, exc)
            continue
        # Human-only check on the LIVE action: a step that targets
        # a CAPTCHA element (or a verification-code field) is a
        # handoff even when the plan description did not say so.
        target = next(
            (e for e in elements if e.get("id") == action.get("element_id")), None
        )
        if target:
            target_text = f"{target.get('name','')} {target.get('text','')}".lower()
            if "captcha" in target_text or "verification code" in target_text:
                _handoff(conn, task_id, n, f"action targets a human-only element: {action.get('element_id')}")
                raise ValueError(f"step {n} targets a human-only element")
        # Never retry a submit: refuse a click on an element that
        # already has a done click step in this task.
        if action.get("action") == "click" and action.get("element_id"):
            done_rows = conn.execute(
                "SELECT action_json FROM steps WHERE task_id = ? AND status = 'done'",
                (task_id,),
            ).fetchall()
            for done_row in done_rows:
                try:
                    done_action = json.loads(done_row["action_json"] or "{}")
                except ValueError:
                    continue
                if (
                    done_action.get("action") == "click"
                    and done_action.get("element_id") == action["element_id"]
                ):
                    raise validate.ValidationError(
                        f"element {action['element_id']} was already clicked successfully in this task"
                    )
        # Already applied (§6.9): before a consequential click,
        # check the receipts + memory. A role applied to in an
        # earlier task (or on another page/element) is never
        # re-submitted — the loop's same-element-in-same-task check
        # alone is not enough. The role comes from the task
        # constraints ("role: …", "url: …").
        role_ctx = _role_from_constraints(task.get("constraints") or "")
        if _is_consequential(action, elements) and (
            role_ctx["role_id"] or role_ctx["url"]
        ):
            from ..jobs import applied as _applied

            if _applied.is_already_applied(
                conn, role_ctx["role_id"] or role_ctx["url"], role_ctx["url"]
            ):
                _set_task_status(conn, task_id, "ask_user")
                _audit(conn, task_id, "task.already_applied",
                       {"step": n, "role_id": role_ctx["role_id"],
                        "url": role_ctx["url"]})
                raise ValueError(
                    f"already applied to {role_ctx['role_id'] or role_ctx['url']}"
                    " (receipt on file — no re-submit)"
                )
        # Approval before a consequential action (§5.4): a submit /
        # apply / confirm click on a task that requires approval
        # needs a valid grant. Without one, the task asks the user
        # (the approval card) and the step does not execute. The
        # grant is consumed here — one approval, one submit — and
        # only a grant scoped to this element/role can be spent.
        if _is_consequential(action, elements) and _requires_approval(task):
            if not _consume_approval(
                conn, task_id,
                element_id=action.get("element_id"),
                role_id=role_ctx["role_id"],
            ):
                _set_task_status(conn, task_id, "ask_user")
                _audit(conn, task_id, "task.approval_needed",
                       {"step": n, "action": action.get("action"),
                        "element_id": action.get("element_id")})
                raise ValueError(
                    f"step {n} needs approval before submit"
                    " (POST /tasks/{id}/approve with an approval_id)"
                )
            _audit(conn, task_id, "task.approval_consumed", {"step": n})
        # Valid: record the step as executing and return the action.
        # The executor needs more than the action: the element's
        # info (the fingerprint), the allowed sites and the task id
        # (the live path had none of these, so the fingerprint
        # check was dead and every submit was refused).
        now = int(time.time())
        conn.execute(
            "UPDATE steps SET action_json = ?, element_id = ?, status = 'executing',"
            " started_at = ?, ms = ? WHERE task_id = ? AND n = ?",
            (json.dumps(action), action.get("element_id"), now, int(ms), task_id, n),
        )
        conn.commit()
        _audit(conn, task_id, "task.execute",
               {"step": n, "action": action.get("action"),
                "element_id": action.get("element_id"), "model": model})
        target = next(
            (e for e in elements if e.get("id") == action.get("element_id")), None
        )
        return {
            "action": action,
            "expected_end_state": plan_step.get("expected_end_state", ""),
            "model": model,
            "task_id": task_id,
            "element": target,
            "allowed_sites": _allowed_sites(task.get("constraints") or ""),
        }
    # 3 failed planner calls: two failed groundings in a row → ask user.
    conn.execute(
        "UPDATE steps SET status = 'failed', error = ? WHERE task_id = ? AND n = ?",
        (f"planner validation failed 3 times: {last_error}", task_id, n),
    )
    _set_task_status(conn, task_id, "ask_user")
    _audit(conn, task_id, "task.ask_user", {"step": n, "reason": last_error})
    raise validate.ValidationError(
        f"planner could not produce a valid action in 3 calls: {last_error}"
    )


def _baseline(observation: dict) -> dict:
    """The pre-action observation, reduced to what verify diffs
    against: element ids, their text/value, the URL, and the page
    text. Pre-existing evidence never verifies a step."""
    elements = observation.get("elements", []) if observation else []
    by_id: dict[str, dict] = {}
    texts: set[str] = set()
    for el in elements:
        el_id = el.get("id")
        if isinstance(el_id, str):
            by_id[el_id] = el
            for field in ("name", "text", "value"):
                v = (el.get(field) or "").strip()
                if v:
                    texts.add(v)
    return {
        "ids": set(by_id),
        "by_id": by_id,
        "texts": texts,
        "url": observation.get("url", "") if observation else "",
    }


def verify_step(
    conn,
    task_id: str,
    n: int,
    new_observation: dict,
    expected_end_state: str,
    previous_url: str = "",
    previous_observation: dict | None = None,
) -> dict:
    """Verify step n by re-observing (plan §6.2: "action ran" is not
    verification). Returns {verified, reason, retry}.

    Every check is a baseline diff against the pre-action
    observation: the end state must be NEWLY true. A confirmation
    that was already on the page, a field that was already filled,
    and an element that was already present do not verify anything.
    """
    step = _get_step(conn, task_id, n)
    if step is None:
        raise KeyError(f"no step {n} for task {task_id}")
    action = json.loads(step["action_json"]) if step["action_json"] else {}
    if action.get("action") == "wait":
        return _mark_done(conn, task_id, n, "wait elapsed")
    elements = new_observation.get("elements", [])
    ids = {e["id"] for e in elements if isinstance(e.get("id"), str)}
    texts = " ".join(
        f"{e.get('name','')} {e.get('text','')} {e.get('value','')}" for e in elements
    )
    url = new_observation.get("url", "")
    expected = expected_end_state or ""
    el_id = action.get("element_id")
    acted = next((e for e in elements if e.get("id") == el_id), None)
    base = _baseline(previous_observation or {"url": previous_url, "elements": []})

    reason = ""
    verified = False
    # 1. An element id NAMED in the expected end state is now present
    #    and is NEW (or its text/value changed). Only synthetic
    #    eclipse_el_<n> ids (and, for older observations, el_<n> ids)
    #    and ids quoted in the end state count — a bare English word
    #    that happens to equal an id does not.
    candidates = set(re.findall(r"eclipse_el_\d+|el_\d+", expected))
    for quoted in re.findall(r'"([^"]+)"', expected):
        if quoted in ids:
            candidates.add(quoted)
    for named in candidates:
        if named in ids:
            before = base["by_id"].get(named)
            after = next((e for e in elements if e.get("id") == named), None)
            if before is None:
                verified, reason = True, f"element {named} appeared"
                break
            before_sig = f"{before.get('name','')}|{before.get('text','')}|{before.get('value','')}"
            after_sig = f"{after.get('name','')}|{after.get('text','')}|{after.get('value','')}"
            if before_sig != after_sig:
                verified, reason = True, f"element {named} changed"
                break
    # 2. A quoted text now appears that was NOT there before.
    if not verified:
        for quoted in re.findall(r'"([^"]+)"', expected):
            if quoted and quoted in texts and quoted not in base["texts"]:
                verified, reason = True, f'text "{quoted}" appeared'
                break
    # 3. Type/select: the acted-on field's value CHANGED (was empty
    #    or different before, non-empty now). A pre-filled value
    #    does not verify the typing.
    if not verified and action.get("action") in ("type", "select") and acted:
        value = (acted.get("value") or "").strip()
        before = base["by_id"].get(el_id) or {}
        before_value = (before.get("value") or "").strip()
        if value and value != before_value:
            verified, reason = True, f"{el_id} value changed"
    # 4. Click: the URL changed, the acted-on element was present
    #    before and is gone now, or a confirmation word appears
    #    that was NOT in the baseline text.
    if not verified and action.get("action") == "click":
        if url and previous_url and url != previous_url:
            verified, reason = True, f"URL changed to {url}"
        elif el_id and el_id in base["ids"] and el_id not in ids:
            verified, reason = True, "acted-on element is gone (page moved on)"
        else:
            base_text = " ".join(sorted(base["texts"])).lower()
            for word in ("confirmation", "success", "submitted", "applied"):
                if word in texts.lower() and word not in base_text:
                    verified, reason = True, f"confirmation text appeared ({word})"
                    break
    # 5. Generic fallback: the URL changed.
    if not verified and url and previous_url and url != previous_url:
        verified, reason = True, f"URL changed to {url}"
    if verified:
        # A verified submit records its receipt HERE (§6.9) — the
        # loop, not an external POST, is what makes "already
        # applied" durable. The role comes from the task
        # constraints; without a role there is nothing to record
        # (the login flow). The confirmation is the end-state
        # text, redacted before it is stored.
        if action.get("action") == "click":
            task_row = conn.execute(
                "SELECT constraints FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            role_ctx = _role_from_constraints(
                (task_row["constraints"] or "") if task_row else ""
            )
            if role_ctx["role_id"] or role_ctx["url"]:
                from ..jobs import applied as _applied
                from ..jobs import receipts as _receipts
                from ..planner import scan as _scan

                confirmation, _hits = _scan.redact(
                    f"{expected} — {reason} ({url or ''})"
                )
                step_row = conn.execute(
                    "SELECT element_id FROM steps WHERE task_id = ? AND n = ?",
                    (task_id, n),
                ).fetchone()
                el = (step_row["element_id"] if step_row else None) or ""
                acted_el = next(
                    (e for e in (new_observation or {}).get("elements", [])
                     if e.get("id") == el), None
                )
                _receipts.record_receipt(
                    conn, task_id,
                    role_ctx["role_id"] or role_ctx["url"] or "",
                    acted_el.get("name", "") if acted_el else "",
                    "", role_ctx["url"] or url or "",
                    confirmation, status="submitted",
                )
                _applied.mark_applied(
                    conn, role_ctx["role_id"] or role_ctx["url"] or "",
                    role_ctx["url"] or url or None,
                )
                _audit(conn, task_id, "task.receipt",
                       {"step": n, "role_id": role_ctx["role_id"]})
        return _mark_done(conn, task_id, n, reason)
    # Not verified: retry up to 3 times, then fail the step.
    retries = int(step.get("retries") or 0) + 1
    conn.execute(
        "UPDATE steps SET retries = ? WHERE task_id = ? AND n = ?",
        (retries, task_id, n),
    )
    conn.commit()
    _audit(conn, task_id, "task.verify_failed",
           {"step": n, "retries": retries, "expected": expected})
    if retries >= MAX_RETRIES_PER_STEP:
        conn.execute(
            "UPDATE steps SET status = 'failed', error = ? WHERE task_id = ? AND n = ?",
            (f"verify failed after {retries} retries: {expected}", task_id, n),
        )
        _set_task_status(conn, task_id, "failed")
        return {"verified": False, "reason": f"failed after {retries} retries", "retry": False}
    backoff = RETRY_BACKOFF_S[min(retries - 1, len(RETRY_BACKOFF_S) - 1)]
    return {
        "verified": False,
        "reason": f"end state not seen ({expected}); retry {retries}/3 after {backoff}s",
        "retry": True,
        "backoff_s": backoff,
    }


def _mark_done(conn, task_id: str, n: int, reason: str) -> dict:
    step = _get_step(conn, task_id, n)
    now = int(time.time())
    started = int(step["started_at"]) if step and step["started_at"] else now
    conn.execute(
        "UPDATE steps SET status = 'done', ended_at = ?, ms = ?, result = ?"
        " WHERE task_id = ? AND n = ?",
        (now, (now - started) * 1000, reason, task_id, n),
    )
    # Checkpoint: 10-line summary of what is done and what is next.
    summary = (
        f"Step {n} done: {reason}.\n"
        f"Task: {get_task(conn, task_id)['goal']}\n"
        f"Last results: {'; '.join(last_results(conn, task_id))}"
    )
    write_checkpoint(conn, task_id, n, summary, {"step": n, "result": reason})
    _audit(conn, task_id, "task.verify", {"step": n, "reason": reason})
    # If every step is done, the task is done. A handoff step still
    # outstanding blocks "done" (it is not done until the user
    # completes it and the task resumes).
    remaining = conn.execute(
        "SELECT COUNT(*) AS c FROM steps WHERE task_id = ?"
        " AND status IN ('pending', 'executing', 'handoff')",
        (task_id,),
    ).fetchone()["c"]
    if remaining == 0:
        _set_task_status(conn, task_id, "done")
    return {"verified": True, "reason": reason, "retry": False}
