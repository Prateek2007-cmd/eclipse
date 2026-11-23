/** Mission driver (PR #5, W2): the live-browser driver the
 * task loop was waiting for (§2b — nothing in the repo drove
 * observe → execute → verify in sequence until this file).
 *
 * The missions alarm (every 15 minutes, see background.ts)
 * pulls due browser jobs from GET /missions/due and drives
 * each one, one step per alarm run per job (a full task takes
 * several alarm cycles — slow but safe; a task's steps are
 * already idempotent-guarded server-side):
 *   observe the tab (eclipse.observe) → POST /tasks/{id}/steps/{n}/execute
 *   → perform the returned action (eclipse.execute) → re-observe
 *   → POST /tasks/{id}/steps/{n}/verify.
 * On approval-needed (execute returns 409 with an approval
 * id), the driver stops that job and calls the notification
 * hook (W3's notifications.ts — imported for real). A job for
 * a missing tab is skipped, not failed (the user has to have
 * the page open for Eclipse to see it — the watch rule).
 *
 * Deps-injected like monitor.ts (checkDueWatches), so the
 * driver is testable with fake Chrome + fake server — see
 * extension/tests/missions.test.mjs.
 */
import { notifyApproval } from "./notifications.js";

export const MISSIONS_ALARM = "eclipse-missions";
export const MISSIONS_PERIOD_MIN = 15;

export type MissionJobKind = "observe" | "execute_step" | "watch_check";

export type MissionDueJob = {
  kind: MissionJobKind;
  task_id?: string;
  step?: number;
  url?: string;
  watch_id?: string;
  selector?: string;
};

export type MissionDue = {
  mission_id: string;
  type: string;
  mission_name?: string;
  job: MissionDueJob;
};

export type Observation = {
  url: string;
  elements: unknown[];
};

export type PlannerAction = {
  action: string;
  element_id?: string;
  text?: string;
  url?: string;
  seconds?: number;
  reason?: string;
};

export type ElementInfo = {
  id: string;
  tag: string;
  role: string;
  name: string;
  text: string;
  sensitive: boolean;
};

export type ExecuteStepResult =
  | {
      ok: true;
      action: PlannerAction;
      expected_end_state: string;
      element?: ElementInfo;
      allowed_sites?: string[];
      fill?: Record<string, string>;
    }
  | {
      ok: false;
      approvalNeeded?: boolean;
      approvalId?: string;
      reobserve?: boolean;
      detail: string;
    };

export type PerformResult = { ok: boolean; detail?: string; url?: string };

export type VerifyResult = {
  verified: boolean;
  reason: string;
  retry: boolean;
};

export type MissionsDeps = {
  /** Fetch the due mission jobs from the server. */
  fetchDue: () => Promise<MissionDue[]>;
  /** Find an open tab whose URL matches the job URL. */
  findTab: (url: string) => Promise<number | null>;
  /** Observe a tab (walk + vision + redaction + gate). */
  observe: (tabId: number) => Promise<Observation>;
  /** Ask the server for the one validated action for a step. */
  executeStep: (
    taskId: string,
    step: number,
    observation: Observation,
  ) => Promise<ExecuteStepResult>;
  /** Perform the validated action in the tab (guarded executor). */
  performAction: (
    tabId: number,
    action: PlannerAction,
    options: {
      taskId?: string;
      allowedSites?: string[];
      expected?: ElementInfo;
      fill?: Record<string, string>;
    },
  ) => Promise<PerformResult>;
  /** Verify the step by re-observation (baseline diff). */
  verifyStep: (
    taskId: string,
    step: number,
    observation: Observation,
    previousObservation: Observation,
    expectedEndState: string,
    previousUrl: string,
  ) => Promise<VerifyResult>;
  /** Check one watch in a tab (the monitor path). */
  checkWatch: (
    job: MissionDueJob,
    tabId: number,
  ) => Promise<{ fired: boolean }>;
  /** Called when a job stops on an approval (default: notify). */
  onApprovalNeeded?: (
    missionName: string,
    detail: string,
    approvalId?: string,
  ) => Promise<void> | void;
};

export type MissionsSummary = {
  jobs: number;
  executed: number;
  verified: number;
  approvalNeeded: number;
  skippedNoTab: number;
  errors: string[];
};

/** The default approval hook: W3's notification (deduped once
 * per approval id inside notifications.ts). */
export async function defaultNotifyApproval(
  missionName: string,
  detail: string,
  approvalId?: string,
): Promise<void> {
  await notifyApproval(missionName, detail, approvalId);
}

async function driveWatchJob(
  deps: MissionsDeps,
  due: MissionDue,
  summary: MissionsSummary,
): Promise<void> {
  const job = due.job;
  if (!job.watch_id || !job.url || !job.selector) {
    summary.errors.push(`${due.mission_id}: watch job has no watch id, url, or selector`);
    return;
  }
  const tabId = await deps.findTab(job.url);
  if (tabId === null) {
    summary.skippedNoTab += 1;
    return;
  }
  await deps.checkWatch(job, tabId);
  summary.executed += 1;
}

async function driveStepJob(
  deps: MissionsDeps,
  due: MissionDue,
  summary: MissionsSummary,
): Promise<void> {
  const job = due.job;
  if (!job.task_id || job.step === undefined) {
    summary.errors.push(`${due.mission_id}: step job has no task id or step`);
    return;
  }
  if (!job.url) {
    // No URL to find a tab for — skip, not fail (the task's
    // constraints carry no role URL; the user can still drive
    // it from the workspace).
    summary.skippedNoTab += 1;
    return;
  }
  const tabId = await deps.findTab(job.url);
  if (tabId === null) {
    summary.skippedNoTab += 1;
    return;
  }
  // 1. Observe.
  let observation = await deps.observe(tabId);
  // 2. Execute (one validated action from the server).
  let exec = await deps.executeStep(job.task_id, job.step, observation);
  if (!exec.ok && exec.reobserve) {
    // Grounding failure (§6.3): re-observe once and re-plan
    // against the fresh observation — never twice.
    observation = await deps.observe(tabId);
    exec = await deps.executeStep(job.task_id, job.step, observation);
  }
  if (!exec.ok) {
    if (exec.approvalNeeded) {
      // The human gate: stop this job and tell the user. The
      // task waits in ask_user until the approval lands (or the
      // runner auto-satisfies it under standing approval).
      summary.approvalNeeded += 1;
      const notify = deps.onApprovalNeeded ?? defaultNotifyApproval;
      await notify(
        due.mission_name ?? due.mission_id,
        exec.detail,
        exec.approvalId,
      );
      return;
    }
    summary.errors.push(
      `${job.task_id} step ${job.step}: ${exec.detail}`,
    );
    return;
  }
  // 3. Perform the action in the tab (guarded executor).
  const performed = await deps.performAction(tabId, exec.action, {
    taskId: job.task_id,
    allowedSites: exec.allowed_sites,
    expected: exec.element,
    fill: exec.fill,
  });
  if (!performed.ok) {
    summary.errors.push(
      `${job.task_id} step ${job.step}: action failed: ${
        performed.detail ?? "unknown error"
      }`,
    );
    return;
  }
  // 4. Re-observe and verify (baseline diff — the pre-action
  // observation is the baseline, so pre-existing text never
  // verifies a step that did nothing).
  const after = await deps.observe(tabId);
  const verdict = await deps.verifyStep(
    job.task_id,
    job.step,
    after,
    observation,
    exec.expected_end_state,
    observation.url,
  );
  summary.executed += 1;
  if (verdict.verified) {
    summary.verified += 1;
  } else {
    summary.errors.push(
      `${job.task_id} step ${job.step}: not verified: ${verdict.reason}`,
    );
  }
}

export async function runDueMissions(
  deps: MissionsDeps,
): Promise<MissionsSummary> {
  const summary: MissionsSummary = {
    jobs: 0,
    executed: 0,
    verified: 0,
    approvalNeeded: 0,
    skippedNoTab: 0,
    errors: [],
  };
  let due: MissionDue[];
  try {
    due = await deps.fetchDue();
  } catch (err) {
    summary.errors.push(
      `could not fetch due missions: ${
        err instanceof Error ? err.message : String(err)
      }`,
    );
    return summary;
  }
  summary.jobs = due.length;
  for (const item of due) {
    try {
      // "observe" jobs are driven like execute_step: the
      // observation feeds the execute call — there is no
      // standalone observe endpoint to report to.
      if (item.job.kind === "watch_check") {
        await driveWatchJob(deps, item, summary);
      } else {
        await driveStepJob(deps, item, summary);
      }
    } catch (err) {
      // B4: one bad job must not abort the whole alarm run.
      summary.errors.push(
        `${item.mission_id}: ${
          err instanceof Error ? err.message : String(err)
        }`,
      );
    }
  }
  return summary;
}
