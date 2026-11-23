/** Notifications + briefing-alarm-time tests (PR #5, W3).
 *
 * No real Chrome, no server: a fake `chrome` global is installed
 * before the modules under test are imported (both register
 * listeners at import time).
 *
 * Run: npm test (pretest compiles src/notifications.ts and
 * src/background.ts to tests-dist/).
 */
import test from "node:test";
import assert from "node:assert/strict";

// ---- Fake chrome -------------------------------------------------

const store = new Map();
const created = []; // chrome.notifications.create calls
const createdTabs = [];
const updatedTabs = [];
const focusedWindows = [];
let clickHandler = null;
let tabsToReturn = [];

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
  tabs: {
    query: async () => tabsToReturn,
    update: async (id, props) => {
      updatedTabs.push([id, props]);
    },
    create: async (props) => {
      createdTabs.push(props);
      return { id: 99 };
    },
  },
  windows: {
    update: async (id, props) => {
      focusedWindows.push([id, props]);
    },
  },
  runtime: {
    getURL: (path) => `chrome-extension://fake/${path}`,
    onMessage: { addListener: () => {} },
    onStartup: { addListener: () => {} },
  },
  alarms: {
    onAlarm: { addListener: () => {} },
    getAll: async () => [],
    create: async () => {},
    clear: async () => {},
  },
};

// The fake must exist before these imports: notifications.ts
// registers the onClicked handler and background.ts registers
// its listeners at module top level.
const {
  notifyApproval,
  notifyBriefing,
  notifyWatchFired,
  istDateString,
} = await import("../tests-dist/notifications.js");
const { minutesUntilBriefing, BRIEFING_HOUR_LOCAL } =
  await import("../tests-dist/background.js");

function reset() {
  store.clear();
  created.length = 0;
  createdTabs.length = 0;
  updatedTabs.length = 0;
  focusedWindows.length = 0;
  tabsToReturn = [];
}

// The click handler fires openWorkspaceTab without awaiting it;
// let the microtasks drain before asserting.
async function flush() {
  await new Promise((resolve) => setTimeout(resolve, 10));
}

// ---- notifyApproval ----------------------------------------------

test("notifyApproval creates a notification naming the mission", async () => {
  reset();
  await notifyApproval("SDE hunt", "Approve the apply to Example Corp", "appr-1");
  assert.equal(created.length, 1);
  assert.equal(created[0].type, "basic");
  assert.match(created[0].title, /SDE hunt/);
  assert.match(created[0].message, /Example Corp/);
  assert.match(created[0].iconUrl, /icons\/eclipse-128\.png$/);
});

test("notifyApproval notifies once per approval id", async () => {
  reset();
  await notifyApproval("SDE hunt", "Approve the apply", "appr-1");
  await notifyApproval("SDE hunt", "Approve the apply", "appr-1");
  assert.equal(created.length, 1);
  assert.equal(store.get("notified:approval:appr-1"), true);
  await notifyApproval("SDE hunt", "Approve another apply", "appr-2");
  assert.equal(created.length, 2);
});

test("notifyApproval without an id dedups on mission + detail", async () => {
  reset();
  await notifyApproval("SDE hunt", "Approve the apply");
  await notifyApproval("SDE hunt", "Approve the apply");
  assert.equal(created.length, 1);
  await notifyApproval("SDE hunt", "Approve something else");
  assert.equal(created.length, 2);
});

// ---- notifyBriefing ----------------------------------------------

test("notifyBriefing creates one notification per IST day", async () => {
  reset();
  await notifyBriefing(4);
  assert.equal(created.length, 1);
  assert.match(created[0].title, /briefing/i);
  assert.match(created[0].message, /4 items/);
  await notifyBriefing(4);
  assert.equal(created.length, 1);
  const todayKey = `notified:briefing:${istDateString()}`;
  assert.equal(store.get(todayKey), true);
});

test("notifyBriefing singularises one item", async () => {
  reset();
  await notifyBriefing(1);
  assert.match(created[0].message, /1 item\./);
});

test("istDateString uses Asia/Kolkata, not UTC", () => {
  // 18:30 UTC is 00:00 IST the next day.
  assert.equal(istDateString(new Date("2026-11-23T18:30:00Z")), "2026-11-24");
  assert.equal(istDateString(new Date("2026-11-23T03:00:00Z")), "2026-11-23");
});

// ---- notifyWatchFired --------------------------------------------

test("notifyWatchFired names the page and does not dedup", async () => {
  reset();
  await notifyWatchFired("https://example.com/results");
  await notifyWatchFired("https://example.com/results");
  // No dedup here: the server already cools a watch down for
  // 1 hour after it fires.
  assert.equal(created.length, 2);
  assert.equal(created[0].message, "https://example.com/results");
});

// ---- click handler -------------------------------------------------

test("clicking a notification opens the workspace tab", async () => {
  reset();
  tabsToReturn = [];
  assert.notEqual(clickHandler, null);
  clickHandler("n1");
  await flush();
  assert.equal(createdTabs.length, 1);
  assert.equal(createdTabs[0].url, "http://127.0.0.1:8000/workspace");
});

test("clicking a notification focuses an open workspace tab", async () => {
  reset();
  tabsToReturn = [{ id: 7, windowId: 3, url: "http://127.0.0.1:8000/workspace" }];
  clickHandler("n1");
  await flush();
  assert.equal(createdTabs.length, 0);
  assert.deepEqual(updatedTabs, [[7, { active: true }]]);
  assert.deepEqual(focusedWindows, [[3, { focused: true }]]);
});

// ---- briefing alarm time (fixed clock) -----------------------------

test("briefing alarm hour is 09:00 local", () => {
  assert.equal(BRIEFING_HOUR_LOCAL, 9);
});

test("minutesUntilBriefing: morning wait is short", () => {
  // Local-constructor dates: timezone-independent.
  assert.equal(minutesUntilBriefing(new Date(2026, 10, 23, 8, 30)), 30);
  assert.equal(minutesUntilBriefing(new Date(2026, 10, 23, 8, 59, 30)), 1);
  assert.equal(minutesUntilBriefing(new Date(2026, 10, 23, 0, 0)), 540);
});

test("minutesUntilBriefing: at or after 09:00 it waits until tomorrow", () => {
  assert.equal(minutesUntilBriefing(new Date(2026, 10, 23, 9, 0)), 1440);
  assert.equal(minutesUntilBriefing(new Date(2026, 10, 23, 10, 0)), 1380);
  assert.equal(minutesUntilBriefing(new Date(2026, 10, 23, 23, 59)), 541);
});
