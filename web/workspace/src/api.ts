/** API client for the Eclipse server (localhost:8000).
 *
 * The token lives in sessionStorage only — it is cleared when
 * the tab closes, never written to a file or a log. Every call
 * sends it as X-Eclipse-Token. The server never stores raw
 * observations, so there is no "raw PII" endpoint to call.
 *
 * Sprint 9 review fix: the first version of this file typed the
 * server responses from memory, and the types were wrong in
 * five places (quota wrapper + keys, flat task detail, proof
 * metric keys, latency shape, /health shape). The server is the
 * contract; the mappings below adapt its real shapes to the
 * shapes the panes render. They are the load-bearing lines of
 * this file — if a server key changes, this file must change
 * with it, not the other way round.
 */
export type TaskSummary = {
  id: string;
  goal: string;
  status: string;
  created_at: number;
};

export type Step = {
  n: number;
  description: string;
  status: string;
  action_json: string | null;
  result: string | null;
  retries: number;
};

export type TaskDetail = {
  task: TaskSummary & { constraints: string };
  steps: Step[];
  checkpoints: unknown[];
  receipts: unknown[];
};

export type ProofMetrics = {
  metrics: {
    grounding: number | null;
    pii_recall: number | null;
    pii_precision: number | null;
    redaction_tokens: number | null;
    resource_mb: number | null;
    latency_p50_s: number | null;
  } | null;
  targets: Record<string, number>;
  honesty: string;
  run_date: string | null;
  fixtures: number | null;
  worst_page: string | null;
  worst_pii_fixture: string | null;
} | null;

export type SentEntry = {
  id: string;
  at: number;
  endpoint: string;
  model: string;
  tokens_est: number;
  ms: number;
  ok: boolean;
  prompt_redacted: string;
  pii_types: string[];
};

export type AuditEntry = {
  id: number;
  task_id: string | null;
  action: string;
  payload_hash: string;
  created_at: number;
};

export type Watch = {
  id: string;
  url: string;
  selector: string;
  condition: string;
  interval_min: number;
  last_value: string | null;
  status: string;
  last_checked: number | null;
  last_fired_at: number | null;
};

export type BriefingCard = {
  kind: string;
  title: string;
  body: string;
  action: string | null;
  action_label: string;
};

/** The server's real shapes (what the endpoints return). */
type ServerQuotaRow = {
  model: string;
  used_today: number;
  limit_day: number;
  reset_at: number;
};

type ServerTask = TaskSummary & {
  constraints: string;
  steps: Step[];
  checkpoints: unknown[];
  receipts: unknown[];
};

type ServerProofMetrics = {
  generated_at: string | null;
  fixtures: number | null;
  metrics: {
    visual_grounding_first_try: number | null;
    pii_recall: number | null;
    pii_precision: number | null;
    redaction_recoverable_tokens: number | null;
    resource_mb: number | null;
    latency_p50_s: number | null;
  } | null;
  targets: Record<string, number>;
  honesty: string;
  worst_page: string | null;
  worst_pii_fixture: string | null;
  reason?: string;
};

type ServerLatency = {
  generated_at: string | null;
  step_time_s: { p50: number | null; mean: number | null; max: number | null } | null;
  latency_target_s: number | null;
  p50_within_target: boolean | null;
  worst_fixture: string | null;
  reason?: string;
};

const TOKEN_KEY = "eclipseToken";

export function getToken(): string | null {
  return sessionStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string): void {
  sessionStorage.setItem(TOKEN_KEY, token);
}

export function clearToken(): void {
  sessionStorage.removeItem(TOKEN_KEY);
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const token = getToken();
  if (!token) throw new Error("no token — paste the server token first");
  const res = await fetch(path, {
    ...init,
    headers: {
      "X-Eclipse-Token": token,
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
  });
  if (res.status === 401) {
    // Sprint 9 fix (review M18): a bad token returns the user
    // to the token gate instead of stranding the panes behind
    // 401 banners. App.tsx listens for this event.
    window.dispatchEvent(new Event("eclipse-unauthorized"));
    throw new Error("unauthorized — check the token");
  }
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status} on ${path}: ${text.slice(0, 200)}`);
  }
  return res.json() as Promise<T>;
}

export const api = {
  health: () =>
    call<{ status: string; service: string; version: string }>("/health"),
  listTasks: () => call<{ tasks: TaskSummary[] }>("/tasks"),
  /** B2: the server returns a FLAT task ({...task, steps,
   * checkpoints, receipts}) — there is no `task` key. Adapt it
   * to the nested shape the pane renders. */
  getTask: async (id: string): Promise<TaskDetail> => {
    const s = await call<ServerTask>(`/tasks/${id}`);
    const { steps, checkpoints, receipts, ...task } = s;
    return { task, steps, checkpoints, receipts };
  },
  createTask: (goal: string, constraints: string) =>
    call<{ id: string }>("/tasks", {
      method: "POST",
      body: JSON.stringify({ goal, constraints }),
    }),
  planTask: (id: string) =>
    call<{ task_id: string; steps: unknown[] }>(`/tasks/${id}/plan`, {
      method: "POST",
    }),
  approveTask: (id: string, approvalId: string) =>
    call(`/tasks/${id}/approve`, {
      method: "POST",
      body: JSON.stringify({ approval_id: approvalId }),
    }),
  cancelTask: (id: string) =>
    call(`/tasks/${id}/cancel`, { method: "POST" }),
  pauseTask: (id: string) =>
    call(`/tasks/${id}/pause`, { method: "POST" }),
  resumeTask: (id: string) =>
    call(`/tasks/${id}/resume`, { method: "POST" }),
  /** B1: the server returns {quota: [{model, used_today,
   * limit_day, reset_at}]} — the wrapper key and the item keys
   * both differ from what the first version typed. */
  quota: async () => {
    const s = await call<{ quota: ServerQuotaRow[] }>("/quota");
    return {
      models: s.quota.map((r) => ({
        name: r.model,
        used_today: r.used_today,
        limit: r.limit_day,
      })),
    };
  },
  /** B4: the server's metric keys are the benchmark JSON keys
   * (visual_grounding_first_try, redaction_recoverable_tokens)
   * and the run date is `generated_at`. Map them. */
  proofMetrics: async (): Promise<ProofMetrics> => {
    const s = await call<ServerProofMetrics>("/proof/metrics");
    if (!s.metrics) {
      return {
        metrics: null,
        targets: s.targets,
        honesty: s.honesty,
        run_date: null,
        fixtures: s.fixtures ?? null,
        worst_page: s.worst_page ?? null,
        worst_pii_fixture: s.worst_pii_fixture ?? null,
      };
    }
    return {
      metrics: {
        grounding: s.metrics.visual_grounding_first_try,
        pii_recall: s.metrics.pii_recall,
        pii_precision: s.metrics.pii_precision,
        redaction_tokens: s.metrics.redaction_recoverable_tokens,
        resource_mb: s.metrics.resource_mb,
        latency_p50_s: s.metrics.latency_p50_s,
      },
      targets: s.targets,
      honesty: s.honesty,
      run_date: s.generated_at,
      fixtures: s.fixtures,
      worst_page: s.worst_page,
      worst_pii_fixture: s.worst_pii_fixture,
    };
  },
  whatWasSent: (q = "") =>
    call<{ entries: SentEntry[] }>(`/proof/what-was-sent?q=${encodeURIComponent(q)}`),
  egress: () =>
    call<{ hits: unknown[]; hit_count: number; checked: number; passed: boolean }>(
      "/proof/egress",
    ),
  redaction: () =>
    call<{ recoverable_tokens: number | null; passed: boolean | null; pii_recall: number | null; pii_precision: number | null }>(
      "/proof/redaction",
    ),
  /** B5: the server returns {step_time_s: {p50, mean, max},
   * latency_target_s, ...} — not {p50_s, target}. Map it. */
  latency: async () => {
    const s = await call<ServerLatency>("/proof/latency");
    return {
      p50_s: s.step_time_s?.p50 ?? null,
      target: s.latency_target_s,
      p50_within_target: s.p50_within_target,
      worst_fixture: s.worst_fixture,
    };
  },
  audit: () => call<{ entries: AuditEntry[] }>("/audit"),
  verifyAudit: () =>
    call<{ ok: boolean; checked: number }>("/audit/verify"),
  vaultStatus: () =>
    call<{ unlocked: boolean; has_grants: boolean }>("/vault/status"),
  watches: () => call<{ watches: Watch[] }>("/monitor/watches"),
  deleteWatch: (id: string) =>
    call<{ deleted: string }>(`/monitor/watches/${id}`, { method: "DELETE" }),
  muteWatch: (id: string, minutes = 60) =>
    call(`/monitor/watches/${id}/mute`, {
      method: "POST",
      body: JSON.stringify({ minutes }),
    }),
  unmuteWatch: (id: string) =>
    call(`/monitor/watches/${id}/unmute`, { method: "POST" }),
  briefingLatest: () =>
    call<{ id: string; created_at: number; cards: BriefingCard[] }>(
      "/monitor/briefing/latest",
    ),
  buildBriefing: () =>
    call<{ id: string; created_at: number; cards: BriefingCard[] }>(
      "/monitor/briefing",
      { method: "POST" },
    ),
};
