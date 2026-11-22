import { useEffect, useState } from "react";
import { api, type TaskDetail, type TaskSummary } from "../api";
import { fmtDateTime, newApprovalId } from "../hooks";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { EmptyState, ErrorBanner, SectionHeader, StatusChip, statusTone } from "../components/ui";

const STEP_ICON: Record<string, string> = {
  pending: "○",
  running: "▶",
  executing: "▶",
  done: "✓",
  failed: "✕",
  skipped: "–",
  handoff: "✋",
};

function useTaskList(token: string | null) {
  const [tasks, setTasks] = useState<TaskSummary[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [lastOk, setLastOk] = useState<number | null>(null);

  useEffect(() => {
    if (!token) {
      setLoading(false);
      return;
    }
    let alive = true;
    const load = async () => {
      try {
        const data = await api.listTasks();
        if (alive) {
          setTasks(data.tasks);
          setError(null);
          setLastOk(Date.now());
        }
      } catch (err) {
        if (alive) setError(err instanceof Error ? err.message : String(err));
      } finally {
        if (alive) setLoading(false);
      }
    };
    void load();
    const timer = setInterval(load, 5000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [token]);

  // Sprint 9 fix (review M13): Retry re-fetches instead of
  // reloading the page (which lost the goal draft + selection).
  const refresh = async () => {
    setLoading(true);
    try {
      const data = await api.listTasks();
      setTasks(data.tasks);
      setError(null);
      setLastOk(Date.now());
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  };

  return { tasks, error, loading, lastOk, refresh };
}

function useTaskDetail(taskId: string | null, token: string | null) {
  const [detail, setDetail] = useState<TaskDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    // Sprint 9 fix (review M14): clear stale detail the moment
    // the selection changes, so the old task's plan never shows
    // under the new task's name.
    setDetail(null);
    setError(null);
    if (!taskId || !token) return;
    let alive = true;
    setLoading(true);
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
    const timer = setInterval(load, 2000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [taskId, token]);

  return { detail, error, loading };
}

/** ApprovalCard (plan §5.4, review M4): what will happen, in
 * plain words, before any consequential step runs. Approve
 * writes the grant; Deny cancels the task. The approval id is
 * generated here — the server accepts any id the client
 * supplies and records it in the grant. */
function ApprovalCard({
  taskId,
  status,
  onApproved,
  onDenied,
}: {
  taskId: string;
  status: string;
  onApproved: () => void;
  onDenied: () => void;
}) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirmDeny, setConfirmDeny] = useState(false);

  const approve = async () => {
    setPending(true);
    setError(null);
    try {
      await api.approveTask(taskId, newApprovalId());
      onApproved();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setPending(false);
    }
  };

  const deny = async () => {
    setConfirmDeny(false);
    setPending(true);
    setError(null);
    try {
      await api.cancelTask(taskId);
      onDenied();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setPending(false);
    }
  };

  return (
    <div className="approval-card" role="group" aria-label="Approval needed">
      <h3>Approval needed</h3>
      <p>
        This task is waiting on you (status: <strong>{status}</strong>). The
        next step is a consequential one — a submit, a payment, or a send.
        Approving lets the task continue from its checkpoint. Nothing is
        sent, charged, or submitted until you approve.
      </p>
      {error && (
        <p role="alert" className="danger">
          {error}
        </p>
      )}
      <div className="task-actions">
        <button type="button" className="primary" onClick={() => void approve()} disabled={pending}>
          {pending ? "Approving…" : "Approve"}
        </button>
        <button type="button" onClick={() => setConfirmDeny(true)} disabled={pending}>
          Deny
        </button>
      </div>
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

/** Agent view: task list + create task + the selected task's
 * plan timeline, approval gate, pause/resume/cancel, grants
 * and receipts. Execution itself runs in the extension — the
 * workspace watches and controls, it does not drive steps. */
export function AgentView({
  token,
  selectedId,
  onSelect,
}: {
  token: string | null;
  selectedId: string | null;
  onSelect: (id: string | null) => void;
}) {
  const { tasks, error, loading, lastOk, refresh } = useTaskList(token);
  const { detail, error: detailError, loading: detailLoading } = useTaskDetail(selectedId, token);
  const [goal, setGoal] = useState("");
  const [constraints, setConstraints] = useState("");
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [actionPending, setActionPending] = useState<string | null>(null);
  const [confirmCancel, setConfirmCancel] = useState(false);
  const [confirmRevoke, setConfirmRevoke] = useState(false);
  const [revoked, setRevoked] = useState<number | null>(null);

  // The revoke note belongs to the task it was about.
  useEffect(() => {
    setRevoked(null);
  }, [selectedId]);

  const create = async (goalText: string) => {
    const text = goalText.trim();
    if (!text) return;
    setCreating(true);
    setCreateError(null);
    try {
      const created = await api.createTask(text, constraints);
      await api.planTask(created.id);
      onSelect(created.id);
      setGoal("");
    } catch (err) {
      setCreateError(err instanceof Error ? err.message : String(err));
    } finally {
      setCreating(false);
    }
  };

  // Sprint 9 fix (review M15): pause/resume/cancel are awaited,
  // with a pending state and visible feedback — a failed click
  // no longer silently does nothing.
  const runAction = async (kind: "pause" | "resume" | "cancel" | "replan") => {
    if (!selectedId) return;
    setActionPending(kind);
    setActionError(null);
    try {
      if (kind === "pause") await api.pauseTask(selectedId);
      if (kind === "resume") await api.resumeTask(selectedId);
      if (kind === "cancel") await api.cancelTask(selectedId);
      if (kind === "replan") await api.planTask(selectedId);
      await refresh();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setActionPending(null);
    }
  };

  const revoke = async () => {
    if (!selectedId) return;
    setConfirmRevoke(false);
    setActionPending("revoke");
    setActionError(null);
    try {
      const result = await api.revokeGrants(selectedId);
      setRevoked(result.revoked);
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setActionPending(null);
    }
  };

  const status = detail?.task.status;

  return (
    <div className="view agent-view">
      <div className="view-head">
        <h2>Agent</h2>
        <p className="muted">
          Tasks run in the extension; this view creates them, watches the
          plan, and holds the approval gate.
        </p>
      </div>

      <section aria-label="Create a task" className="pane">
        <h3>New task</h3>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void create(goal);
          }}
        >
          <label htmlFor="task-goal">What should I do?</label>
          <textarea
            id="task-goal"
            value={goal}
            onChange={(e) => setGoal(e.target.value)}
            placeholder="What should I do?"
            rows={3}
          />
          <label htmlFor="task-constraints">Constraints (sites, budget, do-not-do)</label>
          <input
            id="task-constraints"
            value={constraints}
            onChange={(e) => setConstraints(e.target.value)}
            placeholder="e.g. allowed sites: localhost"
          />
          <div className="chips" role="group" aria-label="Quick tasks">
            {["Find internships", "Cold-mail recruiters", "Watch this page"].map(
              (chip) => (
                <button
                  key={chip}
                  type="button"
                  onClick={() => {
                    setGoal(chip === "Watch this page" ? "Watch this page for changes" : chip);
                  }}
                >
                  {chip}
                </button>
              ),
            )}
          </div>
          <button type="submit" className="primary" disabled={creating || !goal.trim()}>
            {creating ? "Creating…" : "Start task"}
          </button>
          {createError && (
            <p role="alert" className="danger">
              {createError}
            </p>
          )}
        </form>
      </section>

      <div className="split">
        <section aria-label="Tasks" className="pane">
          <SectionHeader title="Tasks" action={<button type="button" onClick={() => void refresh()}>Refresh</button>} />
          {loading && <p className="muted">Loading tasks…</p>}
          {error && <ErrorBanner error={error} lastOk={lastOk} onRetry={() => void refresh()} />}
          {!loading && !error && tasks.length === 0 && (
            <EmptyState>
              No tasks yet. Your first task will look like this: a goal, a
              plan, and a receipt for every consequential step.
            </EmptyState>
          )}
          <ul className="task-list">
            {tasks.map((t) => (
              <li key={t.id}>
                <button
                  type="button"
                  className={t.id === selectedId ? "selected" : ""}
                  onClick={() => onSelect(t.id)}
                  aria-current={t.id === selectedId ? true : undefined}
                >
                  <span>{t.goal}</span>
                  <StatusChip tone={statusTone(t.status)}>{t.status}</StatusChip>
                </button>
              </li>
            ))}
          </ul>
        </section>

        <section aria-label="Task detail" className="pane">
          {!selectedId ? (
            <EmptyState>Pick a task to see its plan, checkpoints and receipts.</EmptyState>
          ) : detailLoading && !detail ? (
            <p className="muted">Loading plan…</p>
          ) : detailError ? (
            <p role="alert" className="danger">
              {detailError}
            </p>
          ) : detail ? (
            <div>
              <SectionHeader
                title={detail.task.goal}
                action={<StatusChip tone={statusTone(detail.task.status)}>{detail.task.status}</StatusChip>}
              />
              <p className="muted mono">
                {detail.task.id} · created {fmtDateTime(detail.task.created_at)}
              </p>
              {detail.task.constraints && (
                <p className="secondary">Constraints: {detail.task.constraints}</p>
              )}

              <h3>Plan</h3>
              {detail.steps.length === 0 ? (
                <EmptyState
                  action={
                    <button
                      type="button"
                      className="primary"
                      onClick={() => void runAction("replan")}
                      disabled={actionPending !== null}
                    >
                      {actionPending === "replan" ? "Planning…" : "Plan this task"}
                    </button>
                  }
                >
                  No plan yet — the planner has not returned steps.
                </EmptyState>
              ) : (
                <ol className="plan-timeline">
                  {detail.steps.map((s) => (
                    <li key={s.n} className={`step-${s.status}`}>
                      <span aria-hidden="true">{STEP_ICON[s.status] ?? "○"}</span>{" "}
                      <span>
                        Step {s.n}: {s.description || "(no description)"}
                      </span>
                      <StatusChip tone={statusTone(s.status)}>{s.status}</StatusChip>
                      {s.error && <span className="danger">{s.error}</span>}
                      {s.result && <span className="muted">{s.result}</span>}
                    </li>
                  ))}
                </ol>
              )}

              {actionError && (
                <p role="alert" className="danger">
                  {actionError}
                </p>
              )}
              <div className="task-actions">
                <button
                  type="button"
                  onClick={() => void runAction("pause")}
                  disabled={
                    actionPending !== null ||
                    (status !== "running" && status !== "planned")
                  }
                >
                  {actionPending === "pause" ? "Pausing…" : "Pause"}
                </button>
                <button
                  type="button"
                  onClick={() => void runAction("resume")}
                  disabled={actionPending !== null || status !== "paused"}
                >
                  {actionPending === "resume" ? "Resuming…" : "Resume"}
                </button>
                <button
                  type="button"
                  onClick={() => setConfirmCancel(true)}
                  disabled={
                    actionPending !== null ||
                    status === "done" ||
                    status === "cancelled"
                  }
                >
                  Cancel
                </button>
                <button
                  type="button"
                  onClick={() => setConfirmRevoke(true)}
                  disabled={actionPending !== null}
                >
                  Revoke grants
                </button>
              </div>
              {revoked !== null && (
                <p role="status" className="muted">
                  Revoked {revoked} approval grant{revoked === 1 ? "" : "s"} for
                  this task.
                </p>
              )}

              {status === "ask_user" && selectedId && (
                <ApprovalCard
                  taskId={selectedId}
                  status={status}
                  onApproved={() => void refresh()}
                  onDenied={() => void refresh()}
                />
              )}

              {detail.checkpoints.length > 0 && (
                <div>
                  <h3>Checkpoints</h3>
                  <ul className="plain-list">
                    {detail.checkpoints.map((c) => (
                      <li key={`${c.step_n}-${c.created_at}`}>
                        <strong>After step {c.step_n}</strong> ·{" "}
                        <span className="muted">{fmtDateTime(c.created_at)}</span>
                        <p>{c.summary}</p>
                      </li>
                    ))}
                  </ul>
                </div>
              )}

              <div>
                <h3>Receipts</h3>
                {detail.receipts.length === 0 ? (
                  <p className="muted">
                    No receipts yet — a receipt appears only when a submit is
                    verified, never before.
                  </p>
                ) : (
                  <ul className="plain-list">
                    {detail.receipts.map((r) => (
                      <li key={r.id}>
                        <StatusChip tone={statusTone(r.status)}>{r.status}</StatusChip>{" "}
                        <strong>{r.title}</strong>
                        {r.company ? ` · ${r.company}` : ""} ·{" "}
                        <span className="muted">{fmtDateTime(r.created_at)}</span>
                        <p className="mono">{r.confirmation}</p>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            </div>
          ) : null}
        </section>
      </div>

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
    </div>
  );
}
