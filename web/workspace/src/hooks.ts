import { useEffect, useState } from "react";
import { api, type TaskSummary } from "./api";

/** Poll a loader on mount and every `intervalMs`. The loader
 * sets its own state; this hook only owns the timer and the
 * alive flag. Pass intervalMs 0 for load-once. */
export function usePoll(
  load: () => Promise<void> | void,
  deps: unknown[],
  intervalMs: number,
) {
  useEffect(() => {
    let alive = true;
    const run = async () => {
      if (!alive) return;
      await load();
    };
    void run();
    if (intervalMs <= 0) {
      return () => {
        alive = false;
      };
    }
    const timer = setInterval(() => void run(), intervalMs);
    return () => {
      alive = false;
      clearInterval(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, intervalMs]);
}

/** The task list, shared by every view that needs a task
 * picker (Mail, Vault, Connectors, Jobs receipts). Polled
 * slowly — pickers do not need live data. */
export function useTaskList(token: string | null) {
  const [tasks, setTasks] = useState<TaskSummary[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!token) return;
    let alive = true;
    const load = async () => {
      try {
        const data = await api.listTasks();
        if (alive) {
          setTasks(data.tasks);
          setError(null);
        }
      } catch (err) {
        if (alive) setError(err instanceof Error ? err.message : String(err));
      }
    };
    void load();
    const timer = setInterval(load, 15000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [token]);

  return { tasks, error };
}

/** A short id for approvals the client generates. The server
 * accepts any id the client supplies and records it in the
 * grant (tasks_api approve, mail_api approve). */
export function newApprovalId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `appr-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

/** Unix seconds → short local time, or "—" for null. */
export function fmtTime(ts: number | null | undefined): string {
  if (ts === null || ts === undefined) return "—";
  return new Date(ts * 1000).toLocaleTimeString();
}

/** Unix seconds → short local date + time, or "—" for null. */
export function fmtDateTime(ts: number | null | undefined): string {
  if (ts === null || ts === undefined) return "—";
  return new Date(ts * 1000).toLocaleString();
}

/** A value the server returns with an uncertain shape (a
 * calendar event's nested start/end) rendered safely. */
export function displayValue(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  if (typeof value === "object") {
    const obj = value as Record<string, unknown>;
    if (typeof obj.dateTime === "string") return obj.dateTime;
    if (typeof obj.date === "string") return obj.date;
    try {
      return JSON.stringify(value);
    } catch {
      return "—";
    }
  }
  return "—";
}
