import { useEffect, useState } from "react";
import { api, type TaskDetail, type TaskSummary } from "../api";

export function useTaskList(token: string | null) {
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

export function useTaskDetail(taskId: string | null, token: string | null) {
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

const STEP_ICON: Record<string, string> = {
  pending: "○",
  running: "▶",
  done: "✓",
  failed: "✕",
  skipped: "–",
};

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

  const approve = async () => {
    setPending(true);
    setError(null);
    try {
      const approvalId =
        typeof crypto !== "undefined" && "randomUUID" in crypto
          ? crypto.randomUUID()
          : `appr-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
      await api.approveTask(taskId, approvalId);
      onApproved();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setPending(false);
    }
  };

  const deny = async () => {
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
        <button type="button" onClick={() => void deny()} disabled={pending}>
          Deny
        </button>
      </div>
    </div>
  );
}

export function TaskPane({
  token,
  selectedId,
  onSelect,
}: {
  token: string | null;
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  const { tasks, error, loading, lastOk, refresh } = useTaskList(token);
  const { detail, error: detailError, loading: detailLoading } = useTaskDetail(selectedId, token);
  const [goal, setGoal] = useState("");
  const [constraints, setConstraints] = useState("");
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [actionPending, setActionPending] = useState<string | null>(null);

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
  const runAction = async (kind: "pause" | "resume" | "cancel") => {
    if (!selectedId) return;
    setActionPending(kind);
    setActionError(null);
    try {
      if (kind === "pause") await api.pauseTask(selectedId);
      if (kind === "resume") await api.resumeTask(selectedId);
      if (kind === "cancel") await api.cancelTask(selectedId);
      await refresh();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setActionPending(null);
    }
  };

  return (
    <section aria-label="Task and plan" className="pane task-pane">
      <h2>Task &amp; Plan</h2>
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

      <h3>Tasks</h3>
      {loading && <p className="muted">Loading tasks…</p>}
      {error && (
        <p role="alert" className="danger">
          {error}{" "}
          {lastOk && (
            <span className="muted">
              Showing data from {new Date(lastOk).toLocaleTimeString()} — it may be stale.
            </span>
          )}{" "}
          <button type="button" onClick={() => void refresh()}>
            Retry
          </button>
        </p>
      )}
      {!loading && !error && tasks.length === 0 && (
        <p className="muted">No tasks yet. Your first task will look like this: a goal, a plan, and a receipt for every consequential step.</p>
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
              <span className="muted mono">{t.status}</span>
              {t.id === selectedId && <span className="selected-mark">● Selected</span>}
            </button>
          </li>
        ))}
      </ul>

      {selectedId && detailLoading && !detail && (
        <p className="muted">Loading plan…</p>
      )}
      {selectedId && detailError && (
        <p role="alert" className="danger">
          {detailError}
        </p>
      )}
      {selectedId && detail && (
        <div>
          <h3>Plan — {detail.task.goal}</h3>
          <p className="secondary">
            Status: <strong>{detail.task.status}</strong>
          </p>
          {detail.steps.length === 0 && (
            <p className="muted">No plan yet — the planner has not returned steps.</p>
          )}
          <ol className="plan-timeline">
            {detail.steps.map((s) => (
              <li key={s.n} className={`step-${s.status}`}>
                <span aria-hidden="true">{STEP_ICON[s.status] ?? "○"}</span>{" "}
                <span>
                  Step {s.n}: {s.description || "(no description)"}
                </span>
                <span className="muted mono">{s.status}</span>
              </li>
            ))}
          </ol>
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
                (detail.task.status !== "running" && detail.task.status !== "planned")
              }
            >
              {actionPending === "pause" ? "Pausing…" : "Pause"}
            </button>
            <button
              type="button"
              onClick={() => void runAction("resume")}
              disabled={actionPending !== null || detail.task.status !== "paused"}
            >
              {actionPending === "resume" ? "Resuming…" : "Resume"}
            </button>
            <button
              type="button"
              onClick={() => void runAction("cancel")}
              disabled={
                actionPending !== null ||
                detail.task.status === "done" ||
                detail.task.status === "cancelled"
              }
            >
              {actionPending === "cancel" ? "Cancelling…" : "Cancel"}
            </button>
          </div>
          {detail.task.status === "ask_user" && (
            <ApprovalCard
              taskId={selectedId}
              status={detail.task.status}
              onApproved={() => void refresh()}
              onDenied={() => void refresh()}
            />
          )}
          {detail.receipts.length > 0 && (
            <div>
              <h3>Receipts</h3>
              <ul>
                {(detail.receipts as { id: string; confirmation: string }[]).map((r) => (
                  <li key={r.id} className="mono">
                    {r.confirmation}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </section>
  );
}
