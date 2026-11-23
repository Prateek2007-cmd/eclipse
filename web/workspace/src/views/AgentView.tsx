import { useEffect, useRef, useState, type RefObject } from "react";
import {
  api,
  type ChatMessage,
  type PendingApproval,
  type Step,
  type TaskDetail,
  type TaskSummary,
} from "../api";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { EclipseHero } from "../components/EclipseHero";
import {
  ApprovalBanner,
  Disclosure,
  EmptyState,
  ErrorBanner,
  HumanError,
  RawData,
  RelativeTime,
  ShowMore,
  Spinner,
  StatusChip,
  humanStatus,
  statusTone,
  SummaryChips,
} from "../components/ui";
import "./AgentView.css";

/** Agent view (PR #6): a real chat thread, like Muse's.
 *
 * Direction (frontend-design / unslop): Eclipse is a
 * mini-Muse — the conversation is the main event, the chrome
 * around it stays quiet. Calm, exact, a little warm. One
 * vermilion accent, only on Send. The signature detail is
 * honesty: every agent reply names its source ("Planned
 * reply" / "Quick reply") in small muted text under the
 * bubble.
 *
 * Two layouts, one composer:
 * - Empty thread: a centered hero — greeting, composer,
 *   suggestion chips. Tapping a chip SENDS it.
 * - Active thread: messages + inline task cards, composer
 *   pinned at the bottom.
 *
 * A greeting NEVER creates a task: every send goes through
 * POST /chat/message only. The server decides whether the
 * message is chat (kind "chat") or starts work (kind
 * "task_created" + task_id). The old task-create endpoint is
 * not called from this view anymore, and the old two-pane
 * layout (task list + "pick a task" detail) is gone.
 *
 * Steering falls out of the normal send path: the composer
 * stays enabled while a task runs, so "stop" or "remote
 * only" go through the same POST, the agent's confirmation
 * renders as a normal bubble, and the anchored task card
 * picks the new state up on its next poll. */

const SUGGESTIONS = ["Find SDE intern roles", "Watch a price", "Draft a cold mail"];

/** Statuses after which a task card stops polling. Anything
 * else (created, planned, running, ask_user, paused, …)
 * keeps polling every 2s so the card never goes silent. */
const TERMINAL_STATUSES = new Set(["done", "cancelled", "failed"]);

function truncate(text: string, max: number): string {
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

/** A plan step as data for the RawData expand: the action
 * JSON parsed back into an object when it parses, so the
 * expand reads as structure, not an escaped string. */
function stepData(s: Step): Record<string, unknown> {
  let action: unknown = s.action_json;
  if (typeof s.action_json === "string") {
    try {
      action = JSON.parse(s.action_json) as unknown;
    } catch {
      action = s.action_json;
    }
  }
  return {
    step: s.n,
    description: s.description,
    status: s.status,
    action,
    element_id: s.element_id,
    result: s.result,
    error: s.error,
    retries: s.retries,
  };
}

/** The failed action behind an action error, so the human
 * error can say what actually failed. */
const ACTION_ERROR_TITLES: Record<string, string> = {
  pause: "Couldn't pause the task",
  resume: "Couldn't resume the task",
  cancel: "Couldn't cancel the task",
  replan: "Couldn't plan this task",
  revoke: "Couldn't revoke the grants",
};

/** Inline button icons (PR #6 Worker 11): one set, 16px,
 * 1.5–2 stroke, currentColor, aria-hidden — the button's
 * label carries the meaning. */
function CheckIcon() {
  return (
    <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M20 6 9 17l-5-5" />
    </svg>
  );
}

function XIcon() {
  return (
    <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
      <path d="M18 6 6 18M6 6l12 12" />
    </svg>
  );
}

function PlusIcon() {
  return (
    <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
      <path d="M12 5v14M5 12h14" />
    </svg>
  );
}

function PauseIcon() {
  return (
    <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
      <path d="M9 5v14M15 5v14" />
    </svg>
  );
}

function PlayIcon() {
  return (
    <svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor" aria-hidden="true">
      <path d="M8 5v14l11-7z" />
    </svg>
  );
}

function ShieldIcon() {
  return (
    <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" aria-hidden="true">
      <path d="M12 3 5 6v5c0 4.5 3 7.5 7 9 4-1.5 7-4.5 7-9V6l-7-3Z" />
    </svg>
  );
}

/** The eclipse glyph: a ring with the occluding disc — the
 * agent's mark, on every agent bubble (see .chat-agent-mark
 * in AgentView.css for the disc colours). */
function AgentMark() {
  return (
    <span className="chat-agent-mark" aria-hidden="true">
      <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">
        <circle className="eclipse-ring" cx="11" cy="12" r="8" fill="none" strokeWidth="1.5" />
        <circle className="eclipse-disc" cx="15.5" cy="9" r="4.5" />
      </svg>
    </span>
  );
}

type ActionKind = "pause" | "resume" | "cancel" | "replan" | "revoke";

/** ApprovalCard (plan §5.4, review M4): what will happen, in
 * plain words, before any consequential step runs. Approve
 * writes the grant; Deny cancels the task. The approval id
 * comes from the server — the task loop records a pending
 * approval when the step reaches the human gate, and the
 * server only accepts that id (Bug 3 fix: a client-made id
 * is a 404). The card loads the live pending approvals and
 * approves the oldest one. Rendered INLINE in the task card,
 * in the thread. */
function ApprovalCard({
  taskId,
  onChanged,
}: {
  taskId: string;
  onChanged: () => void;
}) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [failedAction, setFailedAction] = useState<"approve" | "deny" | null>(null);
  const [confirmDeny, setConfirmDeny] = useState(false);
  const [approvals, setApprovals] = useState<PendingApproval[] | null>(null);

  useEffect(() => {
    let alive = true;
    void api.listPendingApprovals(taskId).then(
      (res) => {
        if (alive) setApprovals(res.approvals);
      },
      (err) => {
        if (alive) setError(err instanceof Error ? err.message : String(err));
      },
    );
    return () => {
      alive = false;
    };
  }, [taskId]);

  const approve = async () => {
    const first = approvals?.[0];
    if (!first) return;
    setPending(true);
    setError(null);
    setFailedAction(null);
    try {
      await api.approveTask(taskId, first.approval_id);
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setFailedAction("approve");
    } finally {
      setPending(false);
    }
  };

  const deny = async () => {
    setConfirmDeny(false);
    setPending(true);
    setError(null);
    setFailedAction(null);
    try {
      await api.cancelTask(taskId);
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setFailedAction("deny");
    } finally {
      setPending(false);
    }
  };

  return (
    <div className="approval-card" role="group" aria-label="Approval needed">
      {approvals === null ? (
        <Spinner label="Checking approvals" />
      ) : approvals.length > 0 ? (
        <ApprovalBanner count={1}>
          <button
            type="button"
            className="btn btn-primary"
            onClick={() => void approve()}
            disabled={pending}
          >
            {pending ? <Spinner label="Approving" /> : <><CheckIcon /> Approve</>}
          </button>
          <button type="button" className="btn" onClick={() => setConfirmDeny(true)} disabled={pending}>
            <XIcon /> Deny
          </button>
        </ApprovalBanner>
      ) : (
        <div className="approval-none">
          <p role="status" className="muted">
            No pending approval for this task — it may have expired or
            already been handled. Denying still cancels the task.
          </p>
          <button type="button" className="btn" onClick={() => setConfirmDeny(true)} disabled={pending}>
            <XIcon /> Deny
          </button>
        </div>
      )}
      <p className="approval-explainer">
        The next step is a consequential one — a submit, a payment, or a
        send. Approving lets the task continue from its checkpoint. Nothing
        is sent, charged, or submitted until you approve.
      </p>
      {approvals && approvals.length > 0 && (
        <p className="secondary">
          Waiting on{" "}
          <strong>
            step {approvals[0].step ?? "—"} — {humanStatus(approvals[0].action)}
          </strong>
          {approvals[0].role_id ? ` for role ${approvals[0].role_id}` : ""}.
        </p>
      )}
      {error && (
        <HumanError
          title={
            failedAction === "deny"
              ? "Couldn't cancel this task"
              : "Couldn't approve this task"
          }
          detail={error}
        />
      )}
      <ConfirmDialog
        open={confirmDeny}
        title="Deny and cancel this task?"
        confirmLabel="Deny and cancel"
        danger
        onConfirm={() => void deny()}
        onCancel={() => setConfirmDeny(false)}
      >
        <p>
          Denying cancels the task. The extension stops working on it, and
          the plan cannot be resumed.
        </p>
      </ConfirmDialog>
    </div>
  );
}

/** A task's live progress, anchored under the chat message
 * that created it. Status chip + goal, plan timeline ("N of
 * M steps done"), the approval gate inline, quiet action row,
 * checkpoints behind a Disclosure, receipts as compact rows.
 * Polls GET /tasks/{id} every 2s while the task is active
 * and stops when it reaches a terminal state — a cancelled
 * task shows "cancelled", never a spinner forever. Full step
 * JSON only behind RawData. */
function TaskCard({
  taskId,
  token,
  onDismiss,
}: {
  taskId: string;
  token: string | null;
  onDismiss?: () => void;
}) {
  const [detail, setDetail] = useState<TaskDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  const [actionError, setActionError] = useState<string | null>(null);
  const [failedAction, setFailedAction] = useState<ActionKind | null>(null);
  const [actionPending, setActionPending] = useState<string | null>(null);
  const [confirmCancel, setConfirmCancel] = useState(false);
  const [confirmRevoke, setConfirmRevoke] = useState(false);
  const [revoked, setRevoked] = useState<number | null>(null);

  // The poll loop reads the latest status from a ref so the
  // interval is created once per task and simply stops
  // fetching once the task is terminal — no requests at all
  // after done/cancelled/failed.
  const terminalRef = useRef(false);
  useEffect(() => {
    terminalRef.current =
      detail !== null && TERMINAL_STATUSES.has(detail.task.status);
  }, [detail]);

  useEffect(() => {
    if (!token) {
      setLoading(false);
      return;
    }
    let alive = true;
    const load = async () => {
      try {
        const data = await api.getTask(taskId);
        if (alive) {
          setDetail(data);
          setError(null);
        }
      } catch (err) {
        if (alive) setError(err instanceof Error ? err.message : String(err));
      } finally {
        if (alive) setLoading(false);
      }
    };
    void load();
    const timer = setInterval(() => {
      if (!terminalRef.current) void load();
    }, 2000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [taskId, token, tick]);

  const refresh = () => setTick((t) => t + 1);

  const runAction = async (kind: "pause" | "resume" | "cancel" | "replan") => {
    setActionPending(kind);
    setActionError(null);
    setFailedAction(null);
    try {
      if (kind === "pause") await api.pauseTask(taskId);
      if (kind === "resume") await api.resumeTask(taskId);
      if (kind === "cancel") await api.cancelTask(taskId);
      if (kind === "replan") await api.planTask(taskId);
      refresh();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
      setFailedAction(kind);
    } finally {
      setActionPending(null);
    }
  };

  const revoke = async () => {
    setConfirmRevoke(false);
    setActionPending("revoke");
    setActionError(null);
    setFailedAction(null);
    try {
      const result = await api.revokeGrants(taskId);
      setRevoked(result.revoked);
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
      setFailedAction("revoke");
    } finally {
      setActionPending(null);
    }
  };

  const status = detail?.task.status;
  const doneSteps = detail
    ? detail.steps.filter((s) => s.status === "done").length
    : 0;
  const failedStep = detail?.steps.find((s) => s.status === "failed");
  const constraintItems = detail
    ? detail.task.constraints
        .split(/[,;]/)
        .map((s) => s.trim())
        .filter(Boolean)
        .map((value) => ({ label: "", value }))
    : [];

  return (
    <article id={`task-${taskId}`} className="task-card" aria-label="Task progress">
      {loading && !detail ? (
        <Spinner label="Loading task" />
      ) : error && !detail ? (
        <HumanError title="Couldn't load this task" detail={error} onRetry={refresh} />
      ) : detail ? (
        <>
          <div className="task-card-head">
            <span className="task-card-goal">{detail.task.goal}</span>
            <StatusChip tone={statusTone(detail.task.status)}>
              {humanStatus(detail.task.status)}
            </StatusChip>
          </div>
          <p className="task-card-meta muted">
            created{" "}
            <RelativeTime ts={detail.task.created_at} />
            {onDismiss && (
              <>
                {" · "}
                <button type="button" className="task-card-dismiss" onClick={onDismiss}>
                  Dismiss
                </button>
              </>
            )}
          </p>
          {constraintItems.length > 0 && (
            <div className="agent-constraints">
              <span className="muted">Constraints</span>
              <SummaryChips items={constraintItems} />
            </div>
          )}

          {status === "ask_user" && (
            <ApprovalCard taskId={taskId} onChanged={refresh} />
          )}

          {status === "failed" && (
            <HumanError
              title="This task failed"
              detail={failedStep?.error ?? undefined}
              onRetry={() => void runAction("replan")}
            />
          )}
          {status === "done" && detail.receipts.length === 0 && (
            <p className="muted task-card-note">
              Finished with no receipts — nothing was submitted.
            </p>
          )}
          {status === "cancelled" && (
            <p className="muted task-card-note">
              Cancelled — steps already done stay done; the rest did not run.
            </p>
          )}

          <h4>Plan</h4>
          {detail.steps.length === 0 ? (
            <EmptyState
              action={
                <button
                  type="button"
                  className="btn btn-primary"
                  onClick={() => void runAction("replan")}
                  disabled={actionPending !== null}
                >
                  {actionPending === "replan" ? (
                    <Spinner label="Planning" />
                  ) : (
                    <><PlusIcon /> Plan this task</>
                  )}
                </button>
              }
            >
              No plan yet — the planner has not returned steps.
            </EmptyState>
          ) : (
            <>
              <p className="muted plan-progress num">
                {doneSteps} of {detail.steps.length} steps done
              </p>
              <ol className="plan-timeline">
                {detail.steps.map((s) => (
                  <li key={s.n} className={`step-${s.status} plan-step`}>
                    <span className="plan-step-num num" aria-hidden="true">
                      {s.n}
                    </span>
                    <span
                      className={`activity-dot tone-${statusTone(s.status)}`}
                      aria-hidden="true"
                    />
                    <div className="plan-step-body">
                      <div className="plan-step-head">
                        <span className="plan-step-desc">
                          {s.description || `Step ${s.n}`}
                        </span>
                        <StatusChip tone={statusTone(s.status)}>{humanStatus(s.status)}</StatusChip>
                      </div>
                      {s.result ? (
                        <span className="plan-step-note muted">
                          {truncate(s.result, 200)}
                        </span>
                      ) : null}
                      {s.error ? (
                        <span className="plan-step-note danger">
                          {truncate(s.error, 200)}
                        </span>
                      ) : null}
                      <RawData label="Step details" data={stepData(s)} />
                    </div>
                  </li>
                ))}
              </ol>
            </>
          )}

          {actionError && (
            <HumanError
              title={
                ACTION_ERROR_TITLES[failedAction ?? "replan"] ??
                "Something went wrong"
              }
              detail={actionError}
              onRetry={
                failedAction === "replan"
                  ? () => void runAction("replan")
                  : undefined
              }
            />
          )}
          <div className="task-actions" aria-label="Task actions">
            <button
              type="button"
              className="btn"
              onClick={() => void runAction("pause")}
              disabled={
                actionPending !== null ||
                (status !== "running" && status !== "planned")
              }
            >
              {actionPending === "pause" ? "Pausing…" : <><PauseIcon /> Pause</>}
            </button>
            <button
              type="button"
              className="btn"
              onClick={() => void runAction("resume")}
              disabled={actionPending !== null || status !== "paused"}
            >
              {actionPending === "resume" ? "Resuming…" : <><PlayIcon /> Resume</>}
            </button>
            <button
              type="button"
              className="btn"
              onClick={() => setConfirmCancel(true)}
              disabled={
                actionPending !== null ||
                status === "done" ||
                status === "cancelled"
              }
            >
              <XIcon /> Cancel
            </button>
            <button
              type="button"
              className="btn"
              onClick={() => setConfirmRevoke(true)}
              disabled={actionPending !== null}
            >
              <ShieldIcon /> Revoke grants
            </button>
          </div>
          {revoked !== null && (
            <p role="status" className="muted">
              Revoked {revoked} approval grant{revoked === 1 ? "" : "s"} for
              this task.
            </p>
          )}

          {detail.checkpoints.length > 0 && (
            <Disclosure summary={`Checkpoints (${detail.checkpoints.length})`}>
              <ul className="plain-list">
                {detail.checkpoints.map((c) => (
                  <li key={`${c.step_n}-${c.created_at}`}>
                    <strong>After step {c.step_n}</strong> ·{" "}
                    <span className="muted">
                      <RelativeTime ts={c.created_at} />
                    </span>
                    <p>{c.summary}</p>
                  </li>
                ))}
              </ul>
            </Disclosure>
          )}

          <div>
            <h4>Receipts</h4>
            {detail.receipts.length === 0 ? (
              <p className="muted">
                No receipts yet — a receipt appears only when a submit is
                verified, never before.
              </p>
            ) : (
              <ul className="plain-list">
                {detail.receipts.map((r) => (
                  <li key={r.id} className="receipt-row">
                    <div className="receipt-head">
                      <StatusChip tone={statusTone(r.status)}>{humanStatus(r.status)}</StatusChip>
                      <strong>{r.title}</strong>
                      {r.company ? (
                        <span className="secondary"> · {r.company}</span>
                      ) : null}
                      <span className="receipt-when muted">
                        <RelativeTime ts={r.created_at} />
                      </span>
                    </div>
                    {r.confirmation ? (
                      r.confirmation.length > 48 ? (
                        <Disclosure
                          summary={
                            <span className="mono">
                              {r.confirmation.slice(0, 48)}…
                            </span>
                          }
                        >
                          <p className="mono receipt-confirm">{r.confirmation}</p>
                        </Disclosure>
                      ) : (
                        <p className="mono receipt-confirm">{r.confirmation}</p>
                      )
                    ) : null}
                  </li>
                ))}
              </ul>
            )}
          </div>
        </>
      ) : null}

      <ConfirmDialog
        open={confirmCancel}
        title="Cancel this task?"
        confirmLabel="Cancel task"
        danger
        onConfirm={() => {
          setConfirmCancel(false);
          void runAction("cancel");
        }}
        onCancel={() => setConfirmCancel(false)}
      >
        <p>
          The task stops where it is. Steps already done stay done; the rest
          will not run.
        </p>
      </ConfirmDialog>
      <ConfirmDialog
        open={confirmRevoke}
        title="Revoke this task's approval grants?"
        confirmLabel="Revoke grants"
        danger
        onConfirm={() => void revoke()}
        onCancel={() => setConfirmRevoke(false)}
      >
        <p>
          Every outstanding approval grant for this task is deleted at once.
          Any step waiting on an approval will need a fresh one.
        </p>
      </ConfirmDialog>
    </article>
  );
}

/** The composer, shared by the hero and the thread layouts —
 * same input, same send behaviour, same IDs (chat-input,
 * chat-send). Only one instance is mounted at a time. The
 * textarea grows to ~5 lines; Enter sends, Shift+Enter is a
 * newline. It is disabled only while a reply POST is in
 * flight — never while a task runs, so steering ("stop",
 * "remote only") always works. */
function ChatComposer({
  draft,
  onDraft,
  sending,
  onSend,
  inputRef,
  pinned,
}: {
  draft: string;
  onDraft: (value: string) => void;
  sending: boolean;
  onSend: (text: string) => void;
  inputRef: RefObject<HTMLTextAreaElement | null>;
  pinned: boolean;
}) {
  // Autogrow, capped at ~5 lines (120px).
  useEffect(() => {
    const el = inputRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 120)}px`;
  }, [draft, inputRef]);

  return (
    <form
      className={pinned ? "chat-composer chat-composer-pinned" : "chat-composer"}
      onSubmit={(e) => {
        e.preventDefault();
        onSend(draft);
      }}
    >
      <label htmlFor="chat-input" className="chat-sr-only">
        Message Eclipse
      </label>
      <textarea
        id="chat-input"
        ref={inputRef}
        value={draft}
        onChange={(e) => onDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            onSend(draft);
          }
        }}
        placeholder="Ask Eclipse to do something, or just say hi"
        rows={1}
      />
      <button
        id="chat-send"
        type="submit"
        className={sending ? "chat-send chat-send-busy" : "chat-send"}
        disabled={sending || !draft.trim()}
        aria-label="Send message"
      >
        {sending ? (
          <Spinner />
        ) : (
          <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <path d="M12 19V5" />
            <path d="m5 12 7-7 7 7" />
          </svg>
        )}
      </button>
    </form>
  );
}

export function AgentView({
  token,
  selectedId,
  onSelect,
}: {
  token: string | null;
  selectedId: string | null;
  onSelect: (id: string | null) => void;
}) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  // Sources for replies received live in this session
  // (history rows may not carry one — see ChatMessage).
  const [sources, setSources] = useState<Record<string, "planner" | "rules">>({});
  const [historyLoading, setHistoryLoading] = useState(true);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState<{
    detail: string;
    retryText: string;
  } | null>(null);
  const [recentTasks, setRecentTasks] = useState<TaskSummary[]>([]);
  const [shownRecent, setShownRecent] = useState(8);
  const inputRef = useRef<HTMLTextAreaElement | null>(null);

  // Auto-scroll: only when the user is already near the
  // bottom — never yank them while they read further up.
  const nearBottomRef = useRef(true);
  useEffect(() => {
    const onScroll = () => {
      const doc = document.documentElement;
      nearBottomRef.current =
        doc.scrollHeight - (window.scrollY + window.innerHeight) < 160;
    };
    window.addEventListener("scroll", onScroll, { passive: true });
    onScroll();
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  useEffect(() => {
    if (!nearBottomRef.current) return;
    window.scrollTo({ top: document.documentElement.scrollHeight, behavior: "smooth" });
  }, [messages, sending, sendError]);

  const loadHistory = async () => {
    setHistoryLoading(true);
    setHistoryError(null);
    try {
      const data = await api.chatHistory(50);
      setMessages(data.messages);
      // First load lands at the latest message, instantly.
      requestAnimationFrame(() => {
        window.scrollTo({ top: document.documentElement.scrollHeight });
      });
    } catch (err) {
      setHistoryError(err instanceof Error ? err.message : String(err));
    } finally {
      setHistoryLoading(false);
    }
  };

  useEffect(() => {
    if (!token) {
      setHistoryLoading(false);
      return;
    }
    void loadHistory();
    // Recent tasks for the disclosure above the thread — a
    // failed load hides the disclosure, it never breaks chat.
    void api.listTasks().then(
      (data) => setRecentTasks(data.tasks),
      () => setRecentTasks([]),
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);

  /** Send one message through POST /chat/message — the only
   * send path. `echo` adds the user's bubble; a retry
   * re-sends without duplicating it. A greeting comes back
   * kind "chat" with no task; a real request comes back kind
   * "task_created" and its card anchors under the reply. */
  const send = async (rawText: string, echo = true) => {
    const text = rawText.trim();
    if (!text || sending) return;
    if (echo) setDraft("");
    setSendError(null);
    if (echo) {
      const userMsg: ChatMessage = {
        id: `local-${Date.now()}`,
        role: "user",
        text,
        kind: "chat",
        task_id: null,
        created_at: Math.floor(Date.now() / 1000),
      };
      setMessages((m) => [...m, userMsg]);
    }
    setSending(true);
    try {
      const res = await api.chatMessage(text);
      const agentMsg: ChatMessage = {
        id: res.message_id,
        role: "agent",
        text: res.reply_text,
        kind: res.kind,
        task_id: res.task_id,
        created_at: Math.floor(Date.now() / 1000),
        source: res.source,
      };
      setMessages((m) => [...m, agentMsg]);
      setSources((s) => ({ ...s, [res.message_id]: res.source }));
      if (res.kind === "task_created" && res.task_id) {
        void api.listTasks().then(
          (data) => setRecentTasks(data.tasks),
          () => undefined,
        );
      }
    } catch (err) {
      setSendError({
        detail: err instanceof Error ? err.message : String(err),
        retryText: text,
      });
    } finally {
      setSending(false);
      // The composer remounts on the hero → thread switch;
      // hand focus back to the fresh input.
      window.setTimeout(() => inputRef.current?.focus(), 60);
    }
  };

  // Task ids already anchored under a message — a task card
  // renders under its FIRST creating message only, even if
  // the same task_id shows up again later.
  const anchored = new Set<string>();
  for (const m of messages) {
    if (m.kind === "task_created" && m.task_id) anchored.add(m.task_id);
  }

  const scrollToTask = (id: string) => {
    window.setTimeout(() => {
      document
        .getElementById(`task-${id}`)
        ?.scrollIntoView({ behavior: "smooth", block: "center" });
    }, 60);
  };

  // A task selected from elsewhere (Missions view, or the
  // Recent tasks disclosure) that has no message anchoring
  // it gets a pinned card instead of vanishing.
  const pinnedCard =
    selectedId !== null && !anchored.has(selectedId) ? (
      <div className="chat-pinned">
        <TaskCard
          taskId={selectedId}
          token={token}
          onDismiss={() => onSelect(null)}
        />
      </div>
    ) : null;

  const isEmpty =
    messages.length === 0 && !historyLoading && !sendError;

  const composer = (
    <ChatComposer
      draft={draft}
      onDraft={setDraft}
      sending={sending}
      onSend={(text) => void send(text)}
      inputRef={inputRef}
      pinned={!isEmpty}
    />
  );

  return (
    <div className="view agent-view">
      {historyLoading ? (
        <div className="chat-loading">
          <Spinner label="Loading conversation" />
        </div>
      ) : isEmpty ? (
        <>
          {historyError && (
            <div className="chat-hero-error">
              <ErrorBanner
                error={historyError}
                onRetry={() => void loadHistory()}
              />
            </div>
          )}
          <div className="chat-hero">
            <EclipseHero />
            <h2 className="chat-hero-title">Hi, I&apos;m Eclipse.</h2>
            <p className="chat-hero-subline muted">
              Ask me to find roles, watch prices, or draft mails — or just
              say hi.
            </p>
            {composer}
            <div className="chips chat-suggestions" role="group" aria-label="Suggestions">
              {SUGGESTIONS.map((s) => (
                <button
                  key={s}
                  type="button"
                  onClick={() => void send(s)}
                  disabled={sending}
                >
                  {s}
                </button>
              ))}
            </div>
            <p className="chat-hero-hint muted">Tap a suggestion to send it.</p>
          </div>
          {pinnedCard}
        </>
      ) : (
        <>
          <div className="view-head">
            <h2>Agent</h2>
            <p className="muted">
              Talk to Eclipse — tasks start here, and their progress lands
              in the thread. Tasks run in the extension; this view watches
              them and holds the approval gate.
            </p>
          </div>

          {recentTasks.length > 0 && (
            <div className="chat-recent">
              <Disclosure summary="Recent tasks">
                <ul className="task-list">
                  {recentTasks.slice(0, shownRecent).map((t) => (
                    <li key={t.id}>
                      <button
                        type="button"
                        className={t.id === selectedId ? "selected" : ""}
                        onClick={() => {
                          onSelect(t.id);
                          scrollToTask(t.id);
                        }}
                        aria-current={t.id === selectedId ? true : undefined}
                      >
                        <span className="task-row-main">
                          <span className="task-row-goal">{t.goal}</span>
                          <span className="task-row-meta">
                            <StatusChip tone={statusTone(t.status)}>
                              {humanStatus(t.status)}
                            </StatusChip>
                            <RelativeTime ts={t.created_at} />
                          </span>
                        </span>
                      </button>
                    </li>
                  ))}
                </ul>
                <ShowMore
                  total={recentTasks.length}
                  shown={Math.min(shownRecent, recentTasks.length)}
                  onShowMore={() => setShownRecent((s) => s + 8)}
                  label="tasks"
                />
              </Disclosure>
            </div>
          )}

          {pinnedCard}

          <div
            id="chat-thread"
            className="chat-thread"
            role="log"
            aria-label="Conversation with Eclipse"
            aria-live="polite"
          >
            {historyError && (
              <ErrorBanner
                error={historyError}
                onRetry={() => void loadHistory()}
              />
            )}
            {messages.map((m) => {
              const isFirstAnchor =
                m.role === "agent" &&
                m.kind === "task_created" &&
                m.task_id !== null &&
                messages.find(
                  (x) =>
                    x.kind === "task_created" && x.task_id === m.task_id,
                )?.id === m.id;
              const source =
                m.role === "agent"
                  ? (sources[m.id] ?? m.source ?? null)
                  : null;
              return (
                <div
                  key={m.id}
                  className={
                    m.role === "user" ? "chat-row chat-row-user" : "chat-row chat-row-agent"
                  }
                >
                  {m.role === "user" ? (
                    <div className="chat-bubble chat-bubble-user">
                      <p>{m.text}</p>
                    </div>
                  ) : (
                    <div className="chat-msg">
                      <AgentMark />
                      <div className="chat-bubble chat-bubble-agent">
                        <p>{m.text}</p>
                      </div>
                    </div>
                  )}
                  <div className="chat-meta">
                    <RelativeTime ts={m.created_at} />
                    {source ? (
                      <span className="chat-source">
                        {source === "planner" ? "Planned reply" : "Quick reply"}
                      </span>
                    ) : null}
                  </div>
                  {isFirstAnchor && m.task_id ? (
                    <div className="chat-task">
                      <TaskCard taskId={m.task_id} token={token} />
                    </div>
                  ) : null}
                </div>
              );
            })}
            {sending && (
              <div className="chat-row chat-row-agent" aria-live="polite">
                <div className="chat-msg">
                  <AgentMark />
                  <div className="chat-bubble chat-bubble-agent chat-thinking">
                    <Spinner label="Eclipse is thinking…" />
                  </div>
                </div>
              </div>
            )}
            {sendError && (
              <HumanError
                title="Couldn't send that message"
                detail={sendError.detail}
                onRetry={() => void send(sendError.retryText, false)}
              />
            )}
          </div>

          {composer}
        </>
      )}
    </div>
  );
}
