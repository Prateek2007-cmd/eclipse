/** Mission driver tests (PR #5, W2).
 *
 * No real Chrome, no server: a fake `chrome` global is installed
 * before the module under test is imported (missions.ts imports
 * notifications.ts, which registers listeners at import time),
 * and every server/tab call is an injected dep.
 *
 * Run: npm test (pretest compiles src/missions.ts to tests-dist/).
 */
import test from "node:test";
import assert from "node:assert/strict";

// ---- Fake chrome -------------------------------------------------

const store = new Map();
const created = []; // chrome.notifications.create calls
let clickHandler = null;

globalThis.chrome = {
  storage: {
    local: {
      get: async (key) => {
        if (typeof key === "string") {
          return store.has(key) ? { [key]: store.get(key) } : {};
        }
        return {};
      },
      set: async (obj) => {
        for (const [k, v] of Object.entries(obj)) store.set(k, v);
      },
    },
    onChanged: { addListener: () => {} },
  },
  notifications: {
    create: async (opts) => {
      created.push(opts);
      return `n${created.length}`;
    },
    onClicked: {
      addListener: (fn) => {
        clickHandler = fn;
      },
    },
  },
  tabs: { query: async () => [], update: async () => {}, create: async () => ({ id: 99 }) },
  windows: { update: async () => {} },
  // createNotification builds the icon URL through runtime.
  runtime: { getURL: (path) => `chrome-extension://fake/${path}` },
};

const { runDueMissions, defaultNotifyApproval, MISSIONS_ALARM, MISSIONS_PERIOD_MIN } =
  await import("../tests-dist/missions.js");

// ---- Deps factory ------------------------------------------------

function makeDeps(overrides = {}) {
  const calls = [];
  const obs = (url) => ({ url, elements: [] });
  const deps = {
    fetchDue: async () => [],
    findTab: async (url) => {
      calls.push(["findTab", url]);
      return 7;
    },
    observe: async (tabId) => {
      calls.push(["observe", tabId]);
      return obs("https://example.com/jobs/role_fit");
    },
    executeStep: async (taskId, step, observation) => {
      calls.push(["executeStep", taskId, step]);
      return {
        ok: true,
        action: { action: "click", element_id: "apply-btn" },
        expected_end_state: "application submitted",
      };
    },
    performAction: async (tabId, action, options) => {
      calls.push(["performAction", tabId, action.action]);
      return { ok: true };
    },
    verifyStep: async (taskId, step, after, before, expected, prevUrl) => {
      calls.push(["verifyStep", taskId, step]);
      return { verified: true, reason: "end state reached", retry: false };
    },
    checkWatch: async (job, tabId) => {
      calls.push(["checkWatch", job.watch_id, tabId]);
      return { fired: false };
    },
    onApprovalNeeded: async (missionName, detail, approvalId) => {
      calls.push(["onApprovalNeeded", missionName, detail, approvalId]);
    },
    ...overrides,
  };
  return { deps, calls };
}

const stepDue = {
  mission_id: "mission_1",
  type: "job_hunt",
  mission_name: "SDE hunt",
  job: {
    kind: "execute_step",
    task_id: "task_1",
    step: 1,
    url: "https://example.com/jobs/role_fit",
  },
};

const watchDue = {
  mission_id: "mission_2",
  type: "price_watch",
  mission_name: "Headphones",
  job: {
    kind: "watch_check",
    watch_id: "watch_1",
    url: "https://example.com/product/1",
    selector: ".price",
  },
};

// ---- Tests -------------------------------------------------------

test("alarm constants", () => {
  assert.equal(MISSIONS_ALARM, "eclipse-missions");
  assert.equal(MISSIONS_PERIOD_MIN, 15);
});

test("observe → execute → perform → observe → verify, in order", async () => {
  const { deps, calls } = makeDeps({
    fetchDue: async () => [stepDue],
  });
  const summary = await runDueMissions(deps);
  assert.equal(summary.jobs, 1);
  assert.equal(summary.executed, 1);
  assert.equal(summary.verified, 1);
  assert.deepEqual(summary.errors, []);
  const kinds = calls.map(([k]) => k);
  assert.deepEqual(kinds, [
    "findTab",
    "observe",
    "executeStep",
    "performAction",
    "observe",
    "verifyStep",
  ]);
});

test("approval-needed stops the job and calls the notification hook", async () => {
  const { deps, calls } = makeDeps({
    fetchDue: async () => [stepDue],
    executeStep: async () => ({
      ok: false,
      approvalNeeded: true,
      approvalId: "appr-abc123",
      detail: "step 2 needs approval before submit",
    }),
  });
  const summary = await runDueMissions(deps);
  assert.equal(summary.approvalNeeded, 1);
  assert.equal(summary.executed, 0);
  assert.equal(summary.verified, 0);
  assert.deepEqual(summary.errors, []);
  // verifyStep and performAction were never reached.
  assert.ok(!calls.some(([k]) => k === "performAction"));
  assert.ok(!calls.some(([k]) => k === "verifyStep"));
  const notified = calls.find(([k]) => k === "onApprovalNeeded");
  assert.deepEqual(notified, [
    "onApprovalNeeded",
    "SDE hunt",
    "step 2 needs approval before submit",
    "appr-abc123",
  ]);
});

test("default approval hook raises a real notification", async () => {
  created.length = 0;
  store.clear();
  await defaultNotifyApproval(
    "SDE hunt",
    "step 2 needs approval before submit",
    "appr-xyz999",
  );
  assert.equal(created.length, 1);
  assert.match(created[0].title, /SDE hunt/);
  assert.match(created[0].message, /approval/);
  // Dedup: the same approval id does not notify twice.
  await defaultNotifyApproval(
    "SDE hunt",
    "step 2 needs approval before submit",
    "appr-xyz999",
  );
  assert.equal(created.length, 1);
});

test("missing tab skips the job without failing it", async () => {
  const { deps, calls } = makeDeps({
    fetchDue: async () => [stepDue],
    findTab: async () => null,
  });
  const summary = await runDueMissions(deps);
  assert.equal(summary.skippedNoTab, 1);
  assert.equal(summary.executed, 0);
  assert.deepEqual(summary.errors, []);
  assert.ok(!calls.some(([k]) => k === "observe"));
});

test("step job with no URL is skipped, not failed", async () => {
  const { deps } = makeDeps({
    fetchDue: async () => [
      { ...stepDue, job: { kind: "execute_step", task_id: "task_1", step: 1 } },
    ],
  });
  const summary = await runDueMissions(deps);
  assert.equal(summary.skippedNoTab, 1);
  assert.deepEqual(summary.errors, []);
});

test("watch_check job calls checkWatch", async () => {
  const { deps, calls } = makeDeps({
    fetchDue: async () => [watchDue],
  });
  const summary = await runDueMissions(deps);
  assert.equal(summary.executed, 1);
  assert.deepEqual(summary.errors, []);
  assert.deepEqual(
    calls.find(([k]) => k === "checkWatch"),
    ["checkWatch", "watch_1", 7],
  );
  assert.ok(!calls.some(([k]) => k === "executeStep"));
});

test("fetchDue failure is recorded, not thrown", async () => {
  const { deps } = makeDeps({
    fetchDue: async () => {
      throw new Error("server down");
    },
  });
  const summary = await runDueMissions(deps);
  assert.equal(summary.jobs, 0);
  assert.equal(summary.errors.length, 1);
  assert.match(summary.errors[0], /server down/);
});

test("reobserve retry: observe again once, then execute again", async () => {
  let executeCalls = 0;
  const { deps, calls } = makeDeps({
    fetchDue: async () => [stepDue],
    executeStep: async () => {
      executeCalls += 1;
      if (executeCalls === 1) {
        return { ok: false, reobserve: true, detail: "grounding failed" };
      }
      return {
        ok: true,
        action: { action: "click", element_id: "apply-btn" },
        expected_end_state: "application submitted",
      };
    },
  });
  const summary = await runDueMissions(deps);
  assert.equal(summary.executed, 1);
  assert.equal(summary.verified, 1);
  assert.deepEqual(summary.errors, []);
  // Initial observe + re-observe + post-action observe.
  assert.equal(calls.filter(([k]) => k === "observe").length, 3);
  assert.equal(executeCalls, 2);
});

test("reobserve twice still fails the job (no infinite retry)", async () => {
  let executeCalls = 0;
  const { deps, calls } = makeDeps({
    fetchDue: async () => [stepDue],
    executeStep: async () => {
      // The override replaces the recording dep, so count here.
      executeCalls += 1;
      return {
        ok: false,
        reobserve: true,
        detail: "grounding failed",
      };
    },
  });
  const summary = await runDueMissions(deps);
  assert.equal(summary.executed, 0);
  assert.equal(summary.errors.length, 1);
  assert.match(summary.errors[0], /grounding failed/);
  // One re-observe only: initial + retry (no post-action).
  assert.equal(calls.filter(([k]) => k === "observe").length, 2);
  assert.equal(executeCalls, 2);
});

test("perform failure is recorded and verify is skipped", async () => {
  const { deps, calls } = makeDeps({
    fetchDue: async () => [stepDue],
    performAction: async () => ({ ok: false, detail: "button not found" }),
  });
  const summary = await runDueMissions(deps);
  assert.equal(summary.executed, 0);
  assert.equal(summary.errors.length, 1);
  assert.match(summary.errors[0], /button not found/);
  assert.ok(!calls.some(([k]) => k === "verifyStep"));
});

test("not-verified step is recorded as an error", async () => {
  const { deps } = makeDeps({
    fetchDue: async () => [stepDue],
    verifyStep: async () => ({
      verified: false,
      reason: "end state not reached",
      retry: true,
    }),
  });
  const summary = await runDueMissions(deps);
  assert.equal(summary.executed, 1);
  assert.equal(summary.verified, 0);
  assert.equal(summary.errors.length, 1);
  assert.match(summary.errors[0], /end state not reached/);
});

test("step job without a task id is an error, not a crash", async () => {
  const { deps } = makeDeps({
    fetchDue: async () => [
      { ...stepDue, job: { kind: "execute_step", step: 1, url: "https://example.com/x" } },
    ],
  });
  const summary = await runDueMissions(deps);
  assert.equal(summary.errors.length, 1);
  assert.match(summary.errors[0], /no task id/);
});

test("one bad job does not abort the whole run", async () => {
  const { deps } = makeDeps({
    fetchDue: async () => [
      { ...stepDue, job: { kind: "execute_step", step: 1, url: "https://example.com/x" } },
      watchDue,
    ],
    observe: async () => {
      throw new Error("observe exploded");
    },
  });
  const summary = await runDueMissions(deps);
  assert.equal(summary.jobs, 2);
  assert.equal(summary.executed, 1); // the watch job still ran
  assert.equal(summary.errors.length, 1);
});
